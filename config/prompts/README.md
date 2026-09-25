# Prompt files

Static halves of agent prompts, kept here so criteria can be changed without
touching Python. Loaders reference them by constructed paths
(`REPO_ROOT / "config" / "prompts" / "fix.md"`), so grepping for a filename does
not find them — hence this table.

| File | Loaded by |
|---|---|
| `fix.md` | `agents/test-healing-agent/actions/01_fix.py` |
| `review.md` | `agents/test-triaging-agent/actions/04_review.py` |
| `explore.md` | `agents/test-adaptation-agent/actions/03_explore_web.py` |
| `adapt.md` | `agents/test-adaptation-agent/actions/04_adapt.py` |

**There is no authoring prompt here.** It is the `build_prompt()` f-string in
`agents/test-authoring-agent/actions/03_generate.py`. There used to be an
`authoring.md`, and it was a trap: a stale, shorter copy that nothing loaded. A
pass to "generalise the authoring prompt" edited it, the diff looked right, and
it changed nothing — the live prompt went on instructing the model to write
`page.locator()`. It was deleted rather than wired up, because loading it would
have dropped the navigation and API-auth guidance the real prompt has and it
does not.

`tests/unit/test_prompt_files.py` enforces that every file here has a loader and
every loader has a file, so that trap cannot be set again.

## System prompt: `config/skills/automation-repo.md`

A different mechanism, same trap. `config/skills/automation-repo.md` is passed
as `--system-prompt-file` — replacing Claude Code's default system prompt — by
the healing fix step (`01_fix.py`), adaptation's web exploration
(`03_explore_web.py`) and adaptation's edit step (`04_adapt.py`). It holds framework-neutral rules only; the target repo's
own `CLAUDE.md` is the source of truth for its APIs. It is not covered by
`test_prompt_files.py`, so keep this list current if you add a loader.
