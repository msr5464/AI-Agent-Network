"""Synthetic failure artifacts, one per diagnosis verdict.

Real DOM snapshots run to half a megabyte, so the corpus is built from small
hand-written pages that carry only the signal each rule keys on. Anything that
depends on the shape of a real capture (the snapshot header, `<body class>`,
locator coverage) is reproduced faithfully; everything else is left out.
"""

_HEADER = ('<!-- qa-agent-network:dom-snapshot test="{test}" url="{url}" '
           'capturedAt="2026-08-26T00:00:00" -->\n')


def snapshot(url: str, title: str, body_class: str = "", body: str = "",
             test: str = "aTest") -> str:
    """A DOM snapshot with the same header the framework writes."""
    return _HEADER.format(test=test, url=url) + (
        f'<!DOCTYPE html><html lang="en"><head><title>{title}</title></head>'
        f'<body class="{body_class}">{body}</body></html>')


# The page a dashboard-style page object expects: its own locators are present.
DASHBOARD_OK = snapshot(
    "https://app.example.com/", "Dashboard · Example", "logged-in",
    '<header><img class="avatar-user" src="a.png">'
    '<summary aria-label="View profile and more">me</summary></header>'
    '<main><h1>Your dashboard</h1></main>')

# The same app, logged out: none of the dashboard locators exist, and a rival
# page object matches instead. This is the shape of the bug that prompted all of
# this — an expired session that presents as a missing element.
DASHBOARD_LOGGED_OUT = snapshot(
    "https://app.example.com/", "Example · Build things", "logged-out",
    '<header><a href="/login">Sign in</a></header>'
    '<main><h1>The future of building</h1></main>')

# Right page, but the avatar element has been renamed. Sibling locators survive,
# which is what separates a genuine locator break from a wrong page.
DASHBOARD_RENAMED = snapshot(
    "https://app.example.com/", "Dashboard · Example", "logged-in",
    '<header><img class="user-photo" src="a.png">'
    '<summary aria-label="View profile and more">me</summary></header>'
    '<main><h1>Your dashboard</h1></main>')

# The element is there, just never visible — covered by a modal.
DASHBOARD_COVERED = snapshot(
    "https://app.example.com/", "Dashboard · Example", "logged-in",
    '<div role="dialog" class="modal-backdrop">Cookies?</div>'
    '<header><img class="avatar-user" src="a.png">'
    '<summary aria-label="View profile and more">me</summary></header>')

# The application served an error page in place of the real one.
ERROR_PAGE = snapshot(
    "https://app.example.com/", "Error · Example", "error",
    '<main><h1>Something went wrong</h1></main>')


DASHBOARD_PAGE_SOURCE = '''
package app.web;
public class DashboardPage extends BasePage {
    private final Locator avatarWidget;
    private final Locator userMenu;
    public DashboardPage(Config config) {
        super(config);
        avatarWidget = page.locator("img[class*='avatar']").first();
        userMenu     = page.locator("summary[aria-label*='View profile']");
        assertPageLoaded(avatarWidget);
    }
}
'''

HOME_PAGE_SOURCE = '''
package app.web;
public class HomePage extends BasePage {
    private final Locator signInButton;
    public HomePage(Config config) {
        super(config);
        signInButton = page.getByRole(AriaRole.LINK,
            new Page.GetByRoleOptions().setName("Sign in"));
        assertPageLoaded(signInButton);
    }
}
'''

# A page object made entirely of XPath: nothing can be evaluated, so the engine
# must abstain rather than read "unevaluable" as "absent".
OPAQUE_PAGE_SOURCE = '''
package app.web;
public class OpaquePage extends BasePage {
    public OpaquePage(Config config) {
        super(config);
        header = page.locator("xpath=//div[@id='header']");
        footer = page.locator("xpath=//div[@id='footer']");
    }
}
'''


def issue(dom_snapshot="", execution_log="", failed_selector="",
          page_object="DashboardPage", **overrides):
    """A handoff issue in the shape both entry points produce."""
    base = {
        "test_name": f"app.tests.SomeTest.verifySomething",
        "error_message": (f"Failed to load Element Locator@{failed_selector} "
                          f"in {page_object}") if failed_selector else "",
        "stack_trace": f"{page_object}.java:19",
        "execution_log": execution_log,
        "dom_snapshot": dom_snapshot,
        "failure_url": "https://app.example.com/",
        "trace_path": "",
        "failed_selector": failed_selector,
    }
    base.update(overrides)
    return base


def context(page_object="DashboardPage", anchors=None, navigation=None,
            coverage=None, ready_state="complete", dom_changed=False,
            elapsed_ms=30029, budget_ms=30000, **overrides):
    """A structured failure context in the shape automation.core.FailureContext writes."""
    payload = {
        "schema": 1, "test": "app.tests.SomeTest.verifySomething",
        "failedAt": "2026-08-26T00:00:00",
        "failure": {"kind": "PAGE_NOT_LOADED", "pageObject": page_object,
                    "anchors": anchors if anchors is not None
                               else [{"selector": "img.avatar", "count": 0, "visible": False}],
                    "elapsedMs": elapsed_ms, "budgetMs": budget_ms},
        "page": {"url": "https://app.example.com/", "title": "T",
                 "bodyClass": "", "readyState": ready_state, "ariaBusy": ""},
        "pageObjectCoverage": {page_object: coverage if coverage is not None
                               else {"matched": 0, "evaluable": 2,
                                     "details": {"avatarWidget": 0, "userMenu": 0}}},
        "domVolatility": {"changedDuringWait": dom_changed},
        "navigation": navigation if navigation is not None
                      else ["https://app.example.com/"],
        "httpErrors": [], "jsErrors": [],
    }
    payload.update(overrides)
    return payload


def baseline_record(page_object="DashboardPage", coverage=None,
                    url="https://app.example.com/", title="Dashboard · Example",
                    body_class="logged-in"):
    """A recorded good-run fingerprint, as automation.core.Baseline writes it."""
    return {"pageObject": page_object, "recordedAt": "2026-08-01T00:00:00",
            "urlShape": url, "title": title, "bodyClass": body_class,
            "coverage": coverage if coverage is not None
                        else {"avatarWidget": 1, "userMenu": 1}}
