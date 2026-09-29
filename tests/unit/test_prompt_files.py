"""Every prompt file must have a loader, and every loader a file.

`config/prompts/` holds the static halves of agent prompts so they can be edited
without touching Python. That only works while each file is actually read by
something — and a file that nothing reads is indistinguishable, in review, from
one that is.

That is not hypothetical. `config/prompts/authoring.md` was a stale, shorter copy
of the prompt built inline in `03_generate.py`. A pass to "generalise the
authoring prompt" edited the file, the diff looked correct, and it changed
nothing: the live prompt went on telling the model to write `page.locator()`
into a repo that might not be Playwright. Nothing failed, because nothing checked.

These tests are that check, in both directions.
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

PROMPT_DIR = REPO_ROOT / "config" / "prompts"
# Where a loader could plausibly live. Not the whole repo: audit directories hold
# copies of past prompts, which would make a dead file look referenced.
CODE_DIRS = ("agents", "shared", "qa_agents_server", "scripts")
CODE_SUFFIXES = (".py", ".sh")


def _source_files():
    for directory in CODE_DIRS:
        root = REPO_ROOT / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if (path.suffix in CODE_SUFFIXES
                    and path.is_file()
                    and "__pycache__" not in path.parts
                    and "audit" not in path.parts):
                yield path


@pytest.fixture(scope="module")
def sources():
    return [(p, p.read_text(encoding="utf-8", errors="ignore")) for p in _source_files()]


def _prompt_files():
    if not PROMPT_DIR.is_dir():
        return []
    # README.md documents the directory; it is not a prompt.
    return sorted(p for p in PROMPT_DIR.glob("*.md") if p.name != "README.md")


@pytest.mark.parametrize("prompt", _prompt_files(), ids=lambda p: p.name)
def test_prompt_file_is_loaded_by_something(prompt, sources):
    """A prompt nothing reads is dead weight that looks alive."""
    referencing = [p.relative_to(REPO_ROOT) for p, text in sources if prompt.name in text]
    assert referencing, (
        f"config/prompts/{prompt.name} is loaded by nothing. Either wire up a "
        f"loader in the same change, or delete it — a prompt file that no code "
        f"reads will be edited by someone expecting it to take effect.")


def test_every_referenced_prompt_file_exists(sources):
    """The reverse: a loader pointing at a file that is not there.

    Catches a rename or delete that missed its loader, which fails at runtime
    inside prompt construction rather than at import.
    """
    pattern = re.compile(r'"prompts"\s*/\s*"([\w.-]+\.md)"')
    missing = []
    for path, text in sources:
        for name in pattern.findall(text):
            if not (PROMPT_DIR / name).is_file():
                missing.append(f"{path.relative_to(REPO_ROOT)} -> config/prompts/{name}")
    assert not missing, "loaders reference prompt files that do not exist: " + "; ".join(missing)


def test_prompt_dir_is_not_empty():
    """Guards the parametrised test above: if the directory were emptied or
    moved, it would collect zero cases and pass while checking nothing."""
    assert _prompt_files(), "no prompt files found — has config/prompts/ moved?"
