"""A located answer has to leave the file compiling.

The run this pins: Locate proved a builder with an option object, the edit was
applied, and the verification run died in the compiler — the page object declared
plain locator fields and had never imported the type the option hangs off. A
build failure names no element, so it read as "the test still fails" and a
locator that was right was put back.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import locator_patch
from shared.edit_guards import apply_edits
from shared.frameworks import get_plugin

BANK_PAGE = '''package shop.web;

import com.microsoft.playwright.Locator;

import shop.core.BasePage;
import shop.core.Config;

public class BankPage extends BasePage {
    private final Locator code;
    public BankPage(Config config) {
        code = page.frameLocator("#pay-frame").locator("#old-code");
    }
}
'''
EXACT_IN_FRAME = ('page.frameLocator("#pay-frame").getByPlaceholder("112233", '
                  'new FrameLocator.GetByPlaceholderOptions().setExact(true))')
ROLE = ('page.getByRole(AriaRole.BUTTON, '
        'new Page.GetByRoleOptions().setName("Pay").setExact(true))')
PLAIN = 'page.frameLocator("#pay-frame").locator("#code")'


class TestWhatASnippetNeeds:
    def test_an_option_object_needs_the_type_it_hangs_off(self):
        code = get_plugin("playwright").code
        assert code.imports_for(EXACT_IN_FRAME) == ["com.microsoft.playwright.FrameLocator"]

    def test_a_role_needs_the_role_enum_too(self):
        code = get_plugin("playwright").code
        assert code.imports_for(ROLE) == ["com.microsoft.playwright.Page",
                                          "com.microsoft.playwright.options.AriaRole"]

    def test_a_plain_locator_needs_nothing(self):
        assert get_plugin("playwright").code.imports_for(PLAIN) == []

    def test_a_framework_that_declares_none_asks_for_none(self):
        assert get_plugin("selenium").code.imports_for(EXACT_IN_FRAME) == []


class TestTheImportTravelsWithTheEdit:
    def test_a_missing_import_is_added_beside_its_family(self):
        edits, error = locator_patch.edits_for(BANK_PAGE, "code", EXACT_IN_FRAME)
        assert not error and len(edits) == 2
        updated, problem = apply_edits(BANK_PAGE, edits)
        assert updated, problem
        assert ("import com.microsoft.playwright.Locator;\n"
                "import com.microsoft.playwright.FrameLocator;\n") in updated
        assert EXACT_IN_FRAME in updated

    def test_an_import_already_there_is_not_added_again(self):
        updated, _ = apply_edits(BANK_PAGE, locator_patch.edits_for(
            BANK_PAGE, "code", EXACT_IN_FRAME)[0])
        assert locator_patch.import_edit(updated, EXACT_IN_FRAME) is None

    def test_a_wildcard_import_covers_its_own_package_only(self):
        wild = BANK_PAGE.replace("import com.microsoft.playwright.Locator;",
                                 "import com.microsoft.playwright.*;")
        assert locator_patch.import_edit(wild, EXACT_IN_FRAME) is None
        # The role enum lives one package down, which the wildcard does not reach.
        added = locator_patch.import_edit(wild, ROLE)
        assert "import com.microsoft.playwright.options.AriaRole;" in added["new_string"]
        assert "import com.microsoft.playwright.Page;" not in added["new_string"]

    def test_a_plain_locator_is_one_edit_as_before(self):
        edits, error = locator_patch.edits_for(BANK_PAGE, "code", PLAIN)
        assert not error and len(edits) == 1

    def test_a_file_with_no_imports_gets_them_under_its_package(self):
        bare = BANK_PAGE.replace("import com.microsoft.playwright.Locator;\n\n", "") \
                        .replace("import shop.core.BasePage;\nimport shop.core.Config;\n\n", "")
        added = locator_patch.import_edit(bare, EXACT_IN_FRAME)
        assert added["old_string"] == "package shop.web;"
        assert added["new_string"].endswith("import com.microsoft.playwright.FrameLocator;")

    def test_the_guarded_writer_adds_it_too(self):
        updated, error = locator_patch.apply_to_source(BANK_PAGE, "code", EXACT_IN_FRAME)
        assert not error
        assert "import com.microsoft.playwright.FrameLocator;" in updated
