"""What a generated test proved about its locators, passing or failing.

The authoring agent's step 04 writes `04-proven-locators.json` when the test passes:
every locator that matched exactly one visible element while it did, named as step 02
names it. Step 02 seeds later runs with these first. It writes
`04-failed-locators.json` when the test fails reproducibly on a step 02 locator.
run.sh keeps both in the step cache.

Step 02 is the only step that reads them, so a step 02 restored from the cache never
sees them. `superseded` says when a passing run has proved something the cached step
02 was never shown, and `failed` when a test failed on a selector it still hands out.
Either way run.sh runs step 02 again instead of restoring it.

CLI (run.sh):
    python3 -m shared.proven_locators current <cache_dir> <01-parse.json>
Exit 0 when the cached step 02 can be restored. Otherwise it prints why and exits 1,
as it also does on any error: a check that cannot be made re-runs step 02 rather than
restoring a possibly stale one.
"""

import json
import sys
from pathlib import Path
from typing import List

from shared.page_identity import qualified_locator_names

FILE = "04-proven-locators.json"
FAILED_FILE = "04-failed-locators.json"
STEP_02 = "02-validate-web.json"
# What step 02 was seeded with. run.sh caches it beside STEP_02.
SEEDS = "02-known-selectors.json"


def selectors(path) -> dict:
    """{name: selector} from a proven or failed locators file, or {} when unreadable."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return {e["name"]: e["selector"] for e in data.get("locators") or []
            if isinstance(e, dict) and e.get("name") and e.get("selector")}


def superseded(cache_dir, plan: dict) -> List[str]:
    """The plan's locator names a passing run proved after the cached step 02 was
    written, for which that step 02 has no selector or a different one.

    Restored, that step 02 hands step 03 the selector step 04 had to replace, or
    none, and step 04 has to find the fix again. A run once proved a page's main
    button, which the cached step 02 had never confirmed, and every later cache hit
    went back to step 03's guess.

    Only a proven file at least as new as the cached step 02 counts, and only a
    selector that step 02 was not already seeded with. Seeded with it, step 02 may
    still report another selector for that element, or none: it chooses. Every
    passing run writes its proof again, so counting those would re-run step 02
    after every passing run, and the cache would never be used. The seeds are
    matched on the selector alone: they hold one entry per selector, so of two
    names sharing one (the same amount on two pages) only the first is listed.
    Only names this plan asks step 02 for count, because step 02 never reports
    any other.
    """
    cache_dir = Path(cache_dir)
    proven_path, web_path = cache_dir / FILE, cache_dir / STEP_02
    if not proven_path.is_file() or proven_path.stat().st_mtime < web_path.stat().st_mtime:
        return []
    asked = set(qualified_locator_names(plan.get("web_pages") or []))
    cached = json.loads(web_path.read_text()).get("selectors") or {}
    try:
        seeded = {e.get("selector") for e in json.loads((cache_dir / SEEDS).read_text())}
    except (OSError, ValueError):
        seeded = set()
    return sorted(name for name, sel in selectors(proven_path).items()
                  if name in asked and cached.get(name) != sel and sel not in seeded)


def failed(cache_dir, plan: dict) -> List[str]:
    """The plan's locator names a test failed on, recorded after the cached step 02
    was written, whose selector that step 02 still hands out.

    Restored, it hands step 03 the selector again, and only a passing run's proof
    ever corrected it. Validated again, a selector the site has since changed
    counts 0 and is replaced; one that still works is confirmed, at the cost of
    one step 02, as with no cache. Once step 02 has run again, what it reported
    stands until a later test fails on it too.
    """
    cache_dir = Path(cache_dir)
    failed_path, web_path = cache_dir / FAILED_FILE, cache_dir / STEP_02
    if not failed_path.is_file() or failed_path.stat().st_mtime < web_path.stat().st_mtime:
        return []
    asked = set(qualified_locator_names(plan.get("web_pages") or []))
    cached = json.loads(web_path.read_text()).get("selectors") or {}
    return sorted(name for name, sel in selectors(failed_path).items()
                  if name in asked and cached.get(name) == sel)


def _cli(argv: List[str]) -> int:
    if len(argv) != 3 or argv[0] != "current":
        print("usage: python3 -m shared.proven_locators current <cache_dir> <01-parse.json>")
        return 2
    plan = json.loads(Path(argv[2]).read_text())
    proven, broke = superseded(argv[1], plan), failed(argv[1], plan)
    reasons = ([f"a passing run has since proved {', '.join(proven)}"] if proven else []) \
        + ([f"a test failed on its {', '.join(broke)}"] if broke else [])
    if reasons:
        print("; ".join(reasons))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
