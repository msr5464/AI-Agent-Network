# Fix Prompt — test-healing-agent

Static half of the locator-fix prompt built by
`agents/test-healing-agent/actions/01_fix.py → build_fix_prompt()`.

Loaded at runtime by `load_fix_rules()`, which takes **everything from the first
`## Instructions` heading onward** and appends it to the generated context. The
text above that heading is documentation and is never sent to the model. If this
file is missing, `_DEFAULT_FIX_RULES` in `01_fix.py` is used instead.

Framework-neutral rules live in `config/skills/automation-repo.md`, passed as
`--system-prompt-file`. The automation repo's own conventions (its `CLAUDE.md` or
equivalent — see `load_repo_conventions()`, up to 64,000 characters) are part of
the generated context above the instructions.

---

## Instructions

0. **Confirm this is the right page before touching any selector.** If a DIAGNOSIS
   section appears above, it has already been worked out from the DOM captured at
   failure, the page object's own locators, the network log and the step timeline.
   Work within it. When it says the failure is not a stale locator, the correct
   answer is `fixable: false` naming that cause — not the closest-looking element
   on a page the test never meant to be on. A selector that "looks right" is how a
   broken login gets shipped green.
1. Identify the EXACT broken locator (CSS selector, XPath, @FindBy, etc.)
2. The broken element is most likely one of the extracted element names above
3. Look in the page object files above for the declaration that needs updating —
   an @FindBy annotation, a `By` constant, or a locator assigned in a constructor
4. If the fix is in a page object file (not the test file), target the page object
5. **IMPORTANT**: Use the wrapper methods from the base class — do NOT call the
   browser driver or locator object directly (`locator.click()`, `element.sendKeys()`,
   `driver.findElement(...)`)
6. **IMPORTANT**: Follow the project conventions shown above
7. Do not refactor, rename, or change anything unrelated to the broken locator

## Output Format (strict)
Respond with a JSON object ONLY. No prose, no markdown fences around it.

```
{
  "fixable": true | false,
  "verdict": "LOCATOR_STALE" | "AMBIGUOUS_LOCATOR" | "STOP",
  "unfixable_reason": "<reason if fixable=false, else null>",
  "fix_description": "<1-2 sentences: what was broken and what you changed>",
  "target_file": "<absolute path of the file to modify>",
  "edits": [
    {
      "old_string": "<exact text to replace — must appear EXACTLY ONCE in the file>",
      "new_string": "<replacement text>"
    }
  ]
}
```

Rules for `verdict`:
- `LOCATOR_STALE` — right page, right state, the element was renamed or moved.
- `AMBIGUOUS_LOCATOR` — the selector now matches several elements; narrow it so
  it matches exactly the intended one.
- **These two are the only verdicts under which an edit is accepted.**
- `STOP` — nothing here is fixable by editing this file: the page was never
  reached, the element exists but was covered or arrived late, the environment
  failed, a fixture was stale. Set `fixable: false` and name which.
- Guards run before the test does. They reject an edit that changes a
  page-load assertion when the diagnosis is not a stale locator, a replacement
  selector that matches nothing (or only hidden elements) in the captured DOM,
  and one that broadens what it replaces.

Rules for `edits`:
- Keep each edit as small as possible — ideally the single locator line.
- `old_string` must match the file byte-for-byte, including indentation, and must
  be unique in the file. Include a line of surrounding context if that is what it
  takes to make it unique.
- **Do NOT return the whole file.** You are shown an excerpt of large files, so a
  regenerated file would silently drop everything you did not see. Whole-file
  responses and oversized diffs are rejected by a safety guard before they are
  applied.
- Do NOT reformat untouched lines.

## Self-Resolving Checklist (before declaring unfixable)

Before setting `fixable: false`, you MUST exhaustively try:

1. Re-read the full execution log and stack trace for the exact failing selector
2. Check all page object files listed above for the declaration matching the element name
3. Try alternative locator strategies in priority order: test id (`[data-cy]`, `[data-testid]`, `[data-test]`) > `#id` > `[name]` > `css` > `xpath`
4. Check related files for alternative element declarations (inner classes, static strings)
5. Look for similar working locators in the same page object as a pattern reference

0. Confirm the page identity first. An element missing because the test never
   arrived is not a locator problem, and no amount of searching will make it one.

If a **LIVE DOM** section appears above, its selectors were observed in a real
browser and verified to match exactly one element — prefer them over anything you
infer from source. If that section says the element is genuinely absent, the right
answer may be `fixable: false` with that explanation, not a guessed selector.

Only declare `fixable: false` after all 5 checks are exhausted and you have a specific blocker.
