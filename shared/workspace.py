"""Find the automation repo, and clone it if it is not there yet.

Three agents needed this and grew three answers. test-healing-agent has a Python
`clone_automation_repo`; test-authoring-agent has a bash block in its run.sh;
test-adaptation-agent had neither and simply refused to run without a checkout.
This is the one implementation, taking healing's version because it is the safer
of the two: `git clone` writes whatever URL it was handed into `.git/config`, so
authoring's `git clone "$_PUSH_URL"` leaves the token in plaintext for the life
of the checkout. Healing strips it back out afterwards, and so does this.

**Syncing is deliberately not a hard reset.** authoring's run.sh follows its clone
with `checkout -f` and `pull`, which is right for an agent that only ever adds new
files — and wrong here: test-adaptation-agent refuses to start on a dirty tree
precisely so that nobody's uncommitted work gets swept into its commit, and a
force-checkout would destroy exactly what that gate protects. So `sync()` fetches,
and leaves the decision about a dirty tree to the caller that already makes it.

**One env var names the checkout.** `FRAMEWORK_DIR` is an absolute path and wins
outright; otherwise the path is `WORKSPACE_DIR/GITHUB_REPO_AUTOMATION`, which is
what every agent computed inline before. The two settings are not
interchangeable: `GITHUB_REPO_AUTOMATION` is also the repo name on GitHub, used
to build clone and PR URLs, so `FRAMEWORK_DIR` overrides where the checkout
lives and nothing else.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Optional


_DIR_ENV = "FRAMEWORK_DIR"
_UNSET = "«FRAMEWORK_DIR-unset»"


def configured() -> Optional[Path]:
    """The checkout path named by FRAMEWORK_DIR, if it is set."""
    value = os.environ.get(_DIR_ENV, "").strip()
    return Path(value).expanduser() if value else None


def expected(workspace_dir, repo_name: str = "") -> Optional[Path]:
    """Where the checkout belongs, whether or not it is there yet.

    Callers that clone into the path, or that report it as missing, need an
    answer before anything exists on disk — `find` cannot give them one. Returns
    None when neither FRAMEWORK_DIR nor both halves of the derived path are set,
    because inventing a default repo name only moves the misconfiguration
    somewhere harder to read.
    """
    override = configured()
    if override is not None:
        return override
    if not (workspace_dir and repo_name):
        return None
    return Path(workspace_dir) / repo_name


def resolve(workspace_dir, repo_name: str = "", exclude=None) -> Path:
    """The checkout path, always a Path so module-level constants stay usable.

    FRAMEWORK_DIR, else WORKSPACE_DIR/GITHUB_REPO_AUTOMATION, else an existing
    sibling checkout matched by shape. With none of those it returns a path that
    cannot exist, so the caller's own "framework repo not found" check names the
    misconfiguration — an import-time crash would take the agent's error
    reporting down with it and say nothing useful.
    """
    return (expected(workspace_dir, repo_name)
            or find(workspace_dir, exclude=exclude)
            or Path(workspace_dir) / _UNSET)


# The only keys this module has any business injecting. Reading config/.env
# wholesale looks harmless and is not: it also carries PLAYWRIGHT_HEADLESS,
# model names, DB credentials and Slack tokens, so a caller that wanted to know
# where the repo lives would silently have its browser mode — and everything
# else — reconfigured underneath it.
_LOCATION_KEYS = (_DIR_ENV, "WORKSPACE_DIR", "GITHUB_REPO_AUTOMATION")


def load_repo_env(root=None) -> None:
    """Read the checkout location out of config/.env, and nothing else.

    For entry points that run outside an agent — the CLIs and the locator_heal
    bench — nothing has sourced the environment yet, so `resolve` would fall
    through to "the first directory with a src/", which on a machine with
    several checkouts is reliably the wrong one. Already-exported values win.
    """
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    for candidate in (root / "config" / ".env", root / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in _LOCATION_KEYS:
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def repo_https_url(org: str, repo: str) -> str:
    return f"https://github.com/{org}/{repo}.git"


def authenticated_url(org: str, repo: str, token: str) -> str:
    """Token-bearing remote URL, for one command only — never written to disk.

    The username is a fixed non-secret placeholder; git needs both halves present
    or it tries to negotiate credentials interactively and fails in a headless
    subprocess. See shared/git.py for the full rationale.
    """
    return f"https://x-access-token:{token}@github.com/{org}/{repo}.git"


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def find(workspace_dir, repo_name: str = "", exclude=None) -> Optional[Path]:
    """An existing checkout under `workspace_dir`, by name or by shape.

    FRAMEWORK_DIR is an assertion, not a hint: if it is set and empty on disk the
    answer is None rather than a repo found by shape-matching somewhere else,
    which would silently work against a checkout nobody asked for.
    """
    override = configured()
    if override is not None:
        return override if override.exists() else None
    root = Path(workspace_dir)
    if repo_name and (root / repo_name).exists():
        return root / repo_name
    if not root.exists():
        return None
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir() or not (candidate / "src").exists():
            continue
        if exclude and candidate.resolve() == Path(exclude).resolve():
            continue
        return candidate
    return None


def clone(workspace_dir, org: str, repo: str, token: str, branch: str = "main",
          log=lambda m: None) -> Optional[Path]:
    """Clone the automation repo. Returns the path, or None with a logged reason."""
    if not (org and repo):
        log("Cannot clone: GITHUB_ORG or GITHUB_REPO_AUTOMATION not set")
        return None
    if not token:
        log("Cannot clone: GITHUB_TOKEN not set")
        return None

    dest = expected(workspace_dir, repo) or Path(workspace_dir) / repo
    log(f"Automation repo not found at {dest} — cloning {org}/{repo} …")
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", branch,
         authenticated_url(org, repo, token), str(dest)],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode != 0:
        log(f"Clone failed: {_redact(result.stderr, token)[:400]}")
        return None

    # git clone persists the URL it was given into .git/config. Strip the token
    # back out so it does not sit in plaintext on disk for the life of the
    # checkout; every later push supplies it per-invocation instead.
    subprocess.run(["git", "remote", "set-url", "origin", repo_https_url(org, repo)],
                   cwd=str(dest), capture_output=True, text=True, timeout=30)
    log(f"Cloned → {dest} (token not persisted in .git/config)")
    return dest


def ensure(workspace_dir, repo: str, org: str = "", token: str = "",
           branch: str = "main", exclude=None, log=lambda m: None) -> Optional[Path]:
    """The automation repo, cloning it only if it is genuinely absent."""
    existing = find(workspace_dir, repo, exclude=exclude)
    if existing is not None:
        return existing
    return clone(workspace_dir, org, repo, token, branch, log)


def sync(path, org: str, repo: str, token: str, branch: str = "main",
         log=lambda m: None) -> Dict:
    """Fetch the default branch. Never force-checks-out, never resets.

    Returns {ok, reason, behind, skipped}. `behind` is how many commits the checkout is
    behind the branch, so the caller can say so rather than silently working from
    a stale base — without taking the decision to move anybody's HEAD.
    """
    path = Path(path)
    # Running against a local checkout with no GitHub configured is a normal
    # development case, not an error. Attempting the fetch anyway produced a
    # confusing "Repository not found" naming a malformed URL, which reads like
    # a real problem with the repo rather than an absent setting.
    if not (org and repo and token):
        return {"ok": True, "behind": None, "skipped": True,
                "reason": "GitHub not configured — using the checkout as it stands"}

    fetched = subprocess.run(
        ["git", "fetch", authenticated_url(org, repo, token), branch],
        cwd=str(path), capture_output=True, text=True, timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if fetched.returncode != 0:
        return {"ok": False, "behind": None,
                "reason": f"git fetch failed: {_redact(fetched.stderr, token)[:200]}"}

    counted = subprocess.run(["git", "rev-list", "--count", "HEAD..FETCH_HEAD"],
                             cwd=str(path), capture_output=True, text=True, timeout=30)
    behind = None
    if counted.returncode == 0 and counted.stdout.strip().isdigit():
        behind = int(counted.stdout.strip())
    return {"ok": True, "behind": behind, "reason": ""}
