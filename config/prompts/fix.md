# Fix Prompt — test-healing-agent

Template for `agents/test-healing-agent/actions/01_fix.py → build_fix_prompt()`.
Loaded at runtime via `Path(REPO_ROOT / "config/prompts/fix.md").read_text()`.

---

## System Role

You are fixing a broken locator in a Selenium/RestAssured test automation file.
Work independently on this test case only.

---

## Instructions

1. Identify the EXACT broken locator (CSS selector, XPath, @FindBy, etc.)
2. The broken element is most likely one of the extracted element names above
3. Look in the page object files above for the @FindBy annotation that needs updating
4. If the fix is in a page object file (not the test file), target the page object
5. **IMPORTANT**: Use the wrapper methods from the base class — do NOT use raw Selenium/RestAssured
6. **IMPORTANT**: Follow the project conventions shown above
7. Do not refactor, rename, or change anything unrelated to the broken locator

---

## Output Format (strict)

Respond with a JSON object ONLY. No prose, no markdown fences around it.

```
{
  "fixable": true | false,
  "unfixable_reason": "<reason if fixable=false, else null>",
  "fix_description": "<1-2 sentences: what was broken and what you changed>",
  "target_file": "<absolute path of the file to modify>",
  "fixed_content": "<complete corrected file content, or null if fixable=false>"
}
```

---

## Self-Resolving Checklist (before declaring unfixable)

Before setting `fixable: false`, you MUST exhaustively try:

1. Re-read the full execution log and stack trace for the exact failing selector
2. Check all page object files listed above for the @FindBy that matches the element name
3. Try alternative locator strategies in priority order: `id` > `name` > `css [data-cy]` > `css` > `xpath`
4. Check related files for alternative element declarations (inner classes, static strings)
5. Look for similar working locators in the same page object as a pattern reference

Only declare `fixable: false` after all 5 checks are exhausted and you have a specific blocker.
