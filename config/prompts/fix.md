# Fix Prompt — test-healing-agent

Static half of the locator-fix prompt built by
`agents/test-healing-agent/actions/01_fix.py → build_fix_prompt()`.

Loaded at runtime by `load_fix_rules()`, which takes **everything from the first
`## Instructions` heading onward** and appends it to the generated context. The
text above that heading is documentation and is never sent to the model. If this
file is missing, `_DEFAULT_FIX_RULES` in `01_fix.py` is used instead.

Domain context (framework patterns, imports, wrapper methods) lives separately in
`config/skills/automation-repo.md`, which is passed as `--system-prompt-file`.

---

## Instructions
1. Identify the EXACT broken locator (CSS selector, XPath, @FindBy, etc.)
2. The broken element is most likely one of the extracted element names above
3. Look in the page object files above for the declaration that needs updating —
   an @FindBy annotation, a `By` constant, or a locator assigned in a constructor
4. If the fix is in a page object file (not the test file), target the page object
5. **IMPORTANT**: Use the wrapper methods from the base class — do NOT use raw Selenium/RestAssured
6. **IMPORTANT**: Follow the project conventions shown above
7. Do not refactor, rename, or change anything unrelated to the broken locator

## Output Format (strict)
Respond with a JSON object ONLY. No prose, no markdown fences around it.

```
{
  "fixable": true | false,
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
3. Try alternative locator strategies in priority order: `id` > `name` > `css [data-cy]` > `css` > `xpath`
4. Check related files for alternative element declarations (inner classes, static strings)
5. Look for similar working locators in the same page object as a pattern reference

If a **LIVE DOM** section appears above, its selectors were observed in a real
browser and verified to match exactly one element — prefer them over anything you
infer from source. If that section says the element is genuinely absent, the right
answer may be `fixable: false` with that explanation, not a guessed selector.

Only declare `fixable: false` after all 5 checks are exhausted and you have a specific blocker.
