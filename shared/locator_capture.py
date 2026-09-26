"""Element fingerprinting.

One page.evaluate() returns a descriptor for every element on the page. The
baseline is the descriptor of the element the locator resolved to on a green
run; the candidate set is every descriptor on the page at failure time. Using
the *same* extractor for both sides is deliberate — if the two disagreed about
how to compute, say, an accessible name, every score would be quietly wrong.

Ground-truth markers (data-gt*) are stripped here so the healer cannot cheat.
"""
from __future__ import annotations
import hashlib, json, os, pathlib
from typing import Any

# Every context the engine opens must match the viewport the framework runs at.
# bbox_norm and area_norm are normalised by the LIVE viewport (locator-capture.js),
# so a baseline recorded by a 1920x1080 maven run and a candidate captured at
# 1280x900 disagree on `location` and `area` for the very same element — an
# identical 200x50 button differs by about 1.8x on area alone. Responsive layouts
# make it worse: 1280 and 1920 can sit on opposite sides of a breakpoint, so the
# two runs are not even looking at the same page.
#
# BrowserHelper.java pins 1920x1080 in every path it opens, and shared/mcp_config
# passes --viewport-size=1920,1080; this is the same number for the same reason.
VIEWPORT = {"width": int(os.environ.get("LOCATOR_VIEWPORT_W", "1920")),
            "height": int(os.environ.get("LOCATOR_VIEWPORT_H", "1080"))}

# The capture script lives in the automation framework and is read from there,
# not copied. It has to ship in the framework's jar — LocatorCapture loads it as
# a resource during a test run, when this repo is not present — so the framework
# is the only place it can be canonical. Keeping a second copy here would mean
# two capture implementations free to drift, and two that disagree corrupt every
# similarity score without anything failing loudly.
#
# Nothing needs it before a heal, and a heal already has the framework checked
# out, so it is loaded on first use rather than at import.
_SCRIPT_RELATIVE = pathlib.Path("src") / "main" / "resources" / "locator-capture.js"
_script_cache: str | None = None


def script_path() -> pathlib.Path:
    """Where the capture script should be, whether or not it is there."""
    from shared import workspace as workspace_helper
    workspace_helper.load_repo_env()
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    framework = workspace_helper.resolve(
        os.environ.get("WORKSPACE_DIR") or repo_root.parent,
        os.environ.get("GITHUB_REPO_AUTOMATION", ""),
        exclude=repo_root)
    return framework / _SCRIPT_RELATIVE


def script_available() -> bool:
    """Whether a checkout carrying the script is reachable — for test guards."""
    return script_path().is_file()


def script() -> str:
    """The capture script, read from the framework checkout and cached."""
    global _script_cache
    if _script_cache is not None:
        return _script_cache

    path = script_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"capture script not found at {path}. It is owned by the automation "
            "framework; point FRAMEWORK_DIR at a checkout, or set WORKSPACE_DIR "
            "and GITHUB_REPO_AUTOMATION.")
    _script_cache = path.read_text()
    return _script_cache



def settle(page, timeout: int = 8000) -> None:
    """Let a page finish arriving before it is judged.

    Baselines are recorded part-way through a test, on a page that had settled.
    Comparing a half-hydrated page against one is how a loading screen gets read
    as a different page, and how a section that renders late reads as removed.
    Best-effort: a page that never goes idle is common and is not a failure.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:                              # noqa: BLE001
        pass


def snapshot(page, attempts: int = 3) -> dict[str, Any]:
    """Fingerprint every element on `page`, plus page-identity context.

    Retries when the execution context is destroyed mid-evaluate. Real
    applications redirect and hydrate after load fires, and the page navigating
    out from under the capture is a timing accident, not an answer — failing here
    would surface as "no elements", which reads exactly like a removed feature.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return page.evaluate(script())
        except Exception as exc:                   # noqa: BLE001 - re-raised below
            message = str(exc)
            if ("Execution context was destroyed" not in message
                    and "navigating" not in message
                    and "navigation" not in message):
                raise
            last = exc
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:                      # noqa: BLE001
                pass
            page.wait_for_timeout(750)
    raise last if last else RuntimeError("snapshot failed")


def scorable(elements: list[dict]) -> list[dict]:
    """Candidate set: visible elements that a test could plausibly target."""
    return [e for e in elements
            if e["is_visible"] and e["area_norm"] > 0
            and e["tag"] not in ("body", "main", "html")]


def find_by_locator(page, raw: str, snap: dict | None = None) -> tuple[int, dict | None]:
    """Resolve `raw` and return (match_count, fingerprint_of_first_match).

    The fingerprint is looked up by position in the *same* filtered node list
    capture.script() walks, so the two views can never drift out of alignment.
    """
    loc = page.locator(raw)
    try:
        n = loc.count()
    except Exception:
        return 0, None          # malformed selector counts as "no match"
    if n == 0:
        return 0, None
    if snap is None:
        snap = snapshot(page)
    # Same expression, element argument: the index comes from the identical walk
    # that produced `snap`, so the two can never disagree.
    idx = page.evaluate(script(), loc.first.element_handle())
    if idx is None or idx < 0 or idx >= len(snap["elements"]):
        return n, None
    return n, snap["elements"][idx]


def resolve_frame(page, raw: str):
    """Which frame does this locator live in? Main frame yields an empty path."""
    for frame in page.frames:
        try:
            if frame.locator(raw).count() > 0:
                path = [] if frame == page.main_frame else [frame.name or frame.url]
                return frame, path
        except Exception:
            continue
    return page.main_frame, []


def element_screenshot(page, raw: str) -> str | None:
    """Small base64 crop of the element, for the PR's before/after."""
    import base64
    try:
        return base64.b64encode(page.locator(raw).first.screenshot(timeout=2000)).decode()
    except Exception:
        return None


def app_commit(cwd: str = ".") -> str | None:
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd,
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def page_digest(landmarks: list[str]) -> str:
    return hashlib.sha1("|".join(sorted(landmarks)).encode()).hexdigest()[:16]


def write_baseline(path: pathlib.Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2))
