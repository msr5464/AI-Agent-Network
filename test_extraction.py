from shared.frameworks import get_active_plugin
from tests import fixtures as fx
locators = get_active_plugin().code.extract_locators(fx.HOME_PAGE_SOURCE)
print("HomePage:", locators)
locators = get_active_plugin().code.extract_locators(fx.DASHBOARD_PAGE_SOURCE)
print("Dashboard:", locators)
