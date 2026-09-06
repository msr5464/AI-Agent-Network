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
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from shared.log import emit


_DIR_ENV = "FRAMEWORK_DIR"
_UNSET = "«FRAMEWORK_DIR-unset»"

# The file a run leaves behind naming the base it actually used. Same shape as
# .verdict / .fix-passed / .crashed: a dot-file in the session's audit dir that
# outlives the process, so analytics and a later retry can both read it.
BASE_MARKER = ".base-branch"


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
# wholesale looks harmless and is not: it also carries HEADLESS_BROWSER,
# model names, DB credentials and Slack tokens, so a caller that wanted to know
# where the repo lives would silently have its browser mode — and everything
# else — reconfigured underneath it.
_LOCATION_KEYS = (_DIR_ENV, "WORKSPACE_DIR", "GITHUB_REPO_AUTOMATION")


def load_repo_env(root=None) -> None:
    """Read the checkout location out of config/.env, and nothing else.

    For entry points that run outside an agent — the CLIs and the locator-eval
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
        log("ERROR: cannot clone — GITHUB_ORG or GITHUB_REPO_AUTOMATION not set")
        return None
    if not token:
        log("ERROR: cannot clone — GITHUB_TOKEN not set")
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
        log(f"ERROR: clone failed — {_redact(result.stderr, token)[:400]}")
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


# ── Basing a run on a branch ──────────────────────────────────────────────────
#
# Every agent used to reach for GITHUB_DEFAULT_BRANCH and hand it straight to
# `git checkout -B x origin/<b>`, which quietly assumed three things that are
# only true for the branch the checkout was cloned on: that origin/<b> exists,
# that it is current, and that the name is safe to pass as an argument. Once a
# run may name its own base, none of the three hold, so they are established
# here once instead of being re-assumed at five call sites.

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_MAX_BRANCH = 200


def normalise_branch(value: str) -> str:
    """A branch name safe to hand to git and to `gh pr create --base`.

    Deliberately narrower than git's own rules. The leading-alphanumeric anchor
    is the load-bearing part: a name starting with "-" is read as a *flag* by
    every command this value reaches, so `--upload-pack=…` typed into a GUI text
    box would otherwise be argument injection. The rest is git's ref grammar
    restated so a bad name fails here, with a message naming the problem, rather
    than as an opaque subprocess error two steps later.

    `git check-ref-format` is deliberately not used: it wants a repository, and
    it *expands* forms like @{-1} — this has to run in the server's build_env,
    where there is no checkout and nothing should be resolved.
    """
    branch = (value or "").strip()
    if not branch:
        raise ValueError("must not be blank")
    if len(branch) > _MAX_BRANCH:
        raise ValueError(f"must be at most {_MAX_BRANCH} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in branch):
        raise ValueError("must not contain control characters")
    if not _BRANCH_RE.match(branch):
        raise ValueError(
            "must start with a letter or digit and contain only letters, "
            "digits, '.', '_', '-' and '/'")
    for bad in ("..", "//", "@{"):
        if bad in branch:
            raise ValueError(f"must not contain {bad!r}")
    if branch.endswith(("/", ".", ".lock")):
        raise ValueError("must not end with '/', '.' or '.lock'")
    for segment in branch.split("/"):
        if not segment:
            raise ValueError("must not contain an empty path segment")
        if segment.startswith("."):
            raise ValueError("no path segment may start with '.'")
        if segment.endswith(".lock"):
            raise ValueError("no path segment may end with '.lock'")
    return branch


def _git(args: List[str], cwd=None, timeout: int = 60):
    """git with prompts disabled — spelled out three times before this existed."""
    return subprocess.run(["git", *args],
                          cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def remote_branch_exists(org: str, repo: str, token: str,
                         branch: str) -> Optional[bool]:
    """Does `branch` exist on the remote? None when the question cannot be asked.

    `ls-remote` needs no checkout, so the server can call this at request time
    and reject a typo before a session directory exists. None rather than False
    for an unreachable remote is the whole point of the tri-state: refusing to
    start a run because the network blinked would be worse than letting the
    agent's own pre-flight decide.
    """
    if not (org and repo and token):
        return None
    try:
        branch = normalise_branch(branch)
    except ValueError:
        return False
    try:
        # The full refs/heads/<b>, not a bare name: `ls-remote --heads <url> main`
        # also matches refs/heads/foo/main, which would wave a typo through.
        result = _git(["ls-remote", "--exit-code", "--heads",
                       authenticated_url(org, repo, token), f"refs/heads/{branch}"],
                      timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    # 2 is ls-remote's documented "no matching refs". Anything else is a
    # transport or auth failure, which is not evidence about the branch.
    return False if result.returncode == 2 else None


def list_remote_branches(org: str, repo: str, token: str) -> List[str]:
    """Every branch on the remote, for the UI's picker. [] on any failure."""
    if not (org and repo and token):
        return []
    try:
        result = _git(["ls-remote", "--heads", authenticated_url(org, repo, token)],
                      timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    prefix = "refs/heads/"
    names = {ref[len(prefix):] for _sha, _tab, ref in
             (line.partition("\t") for line in result.stdout.splitlines())
             if ref.startswith(prefix)}
    return sorted(names)


def write_base_marker(audit_dir, branch: str, sha: str) -> None:
    """Record the base this run actually used, for analytics and for retries.

    Best-effort by design: a run that cannot write its marker should still run.
    """
    if not audit_dir:
        return
    try:
        Path(audit_dir).mkdir(parents=True, exist_ok=True)
        (Path(audit_dir) / BASE_MARKER).write_text(f"{branch}\n{sha}\n")
    except OSError:
        pass


def read_base_marker(audit_dir) -> Tuple[str, str]:
    """(branch, sha) from a session's marker, ("", "") when there isn't one."""
    try:
        lines = (Path(audit_dir) / BASE_MARKER).read_text().splitlines()
    except (OSError, TypeError):
        return "", ""
    branch = lines[0].strip() if lines else ""
    sha = lines[1].strip() if len(lines) > 1 else ""
    return branch, sha


def prepare_base(path, org: str, repo: str, token: str, branch: str,
                 log=lambda m: None) -> Dict:
    """Make `origin/<branch>` exist and be current. Never moves HEAD.

    Returns {ok, branch, ref, sha, reason}. Not moving HEAD is what lets
    test-adaptation-agent call this *before* its cleanliness gate — the same
    reason sync() refuses to reset (see this module's docstring). Callers that
    do want the working tree moved call checkout_base() afterwards.

    Three things go wrong without this, none of them visible while every run
    uses the branch the checkout was cloned on:

      * `run_git(["fetch", "origin"], push_url=U)` substitutes the literal
        "origin" argument, so it runs `git fetch <URL>` with no refspec. That
        populates FETCH_HEAD and leaves refs/remotes/origin/* untouched, so a
        following `checkout -B x origin/<b>` silently used the *clone-time* ref.
      * clone() uses `--depth 1 --branch <b>`, a single-branch clone whose
        remote.origin.fetch refspec covers that one branch. origin/<other> does
        not exist and cannot be fetched by a bare `git fetch`.
      * A branch created after the clone has no remote-tracking ref at all.

    An explicit destination refspec fixes all three at once.
    """
    try:
        branch = normalise_branch(branch)
    except ValueError as e:
        return {"ok": False, "branch": branch, "ref": "", "sha": "",
                "reason": f"invalid branch name: {e}"}

    ref = f"refs/remotes/origin/{branch}"

    def _resolve() -> str:
        got = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                   cwd=path, timeout=30)
        return got.stdout.strip() if got.returncode == 0 else ""

    # Running against a local checkout with no GitHub configured is a normal
    # development case, not an error — sync() already recognises it. Answer from
    # whatever refs are on disk rather than building a malformed URL.
    if not (org and repo and token):
        sha = _resolve()
        if not sha:
            local = _git(["rev-parse", "--verify", "--quiet",
                          f"refs/heads/{branch}^{{commit}}"], cwd=path, timeout=30)
            sha = local.stdout.strip() if local.returncode == 0 else ""
            ref = f"refs/heads/{branch}" if sha else ref
        if not sha:
            return {"ok": False, "branch": branch, "ref": "", "sha": "",
                    "reason": (f"GitHub is not configured and '{branch}' is not "
                               f"in this checkout")}
        write_base_marker(os.environ.get("AUDIT_DIR", ""), branch, sha)
        log(f"Base branch: {branch} @ {sha[:8]} (local — GitHub not configured)")
        return {"ok": True, "branch": branch, "ref": ref, "sha": sha, "reason": ""}

    url = authenticated_url(org, repo, token)

    # Ask before fetching, so a typo fails in a second with a sentence a human
    # can act on rather than after a multi-minute clone-sized transfer.
    try:
        seen = _git(["ls-remote", "--exit-code", "--heads", url,
                     f"refs/heads/{branch}"], timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "branch": branch, "ref": "", "sha": "",
                "reason": f"could not reach {org}/{repo}: {e}"}
    if seen.returncode == 2:
        return {"ok": False, "branch": branch, "ref": "", "sha": "",
                "reason": f"branch '{branch}' does not exist in {org}/{repo}"}
    if seen.returncode != 0:
        # Deliberately a different sentence from the one above: "unreachable"
        # and "no such branch" send a reader to different places.
        return {"ok": False, "branch": branch, "ref": "", "sha": "",
                "reason": (f"could not reach {org}/{repo}: "
                           f"{_redact(seen.stderr, token)[:200]}")}

    shallow = _git(["rev-parse", "--is-shallow-repository"], cwd=path, timeout=30)
    is_shallow = shallow.returncode == 0 and shallow.stdout.strip() == "true"

    # --depth 1 ONLY when the checkout is already shallow. On a complete clone
    # it writes .git/shallow and truncates the history of a checkout three
    # agents share — a silent, permanent degradation. --unshallow is not used
    # either: nothing here needs full history, and pushing from a shallow clone
    # already works (it is how the healing agent ships today).
    fetch_args = ["fetch", "--no-tags"]
    if is_shallow:
        fetch_args += ["--depth", "1"]
    # The explicit destination is the entire point — it overrides the
    # single-branch remote.origin.fetch that `clone --depth 1 --branch` wrote,
    # so the ref lands in refs/remotes/ instead of vanishing into FETCH_HEAD.
    # The leading + lets a force-pushed base branch still update.
    fetch_args += [url, f"+refs/heads/{branch}:{ref}"]
    fetched = _git(fetch_args, cwd=path, timeout=300)
    if fetched.returncode != 0:
        return {"ok": False, "branch": branch, "ref": "", "sha": "",
                "reason": (f"git fetch of {branch} failed: "
                           f"{_redact(fetched.stderr, token)[:200]}")}

    sha = _resolve()
    if not sha:
        # Belt and braces: report it here rather than letting the caller's
        # checkout -B fail with a message about a ref nobody mentioned.
        return {"ok": False, "branch": branch, "ref": ref, "sha": "",
                "reason": f"fetched {branch} but {ref} still does not resolve"}

    write_base_marker(os.environ.get("AUDIT_DIR", ""), branch, sha)
    log(f"Base branch: {branch} @ {sha[:8]}")
    return {"ok": True, "branch": branch, "ref": ref, "sha": sha, "reason": ""}


def checkout_base(path, branch: str, start_point: str = "",
                  log=lambda m: None) -> Dict:
    """Put the checkout ON `branch`. DESTROYS uncommitted work — gate on that.

    One command where callers previously ran `checkout -f`, then on failure a
    `fetch`, then a retry, then a `pull`. It is strictly better than that
    sequence: it works when no *local* branch of the name exists yet (the exact
    case the old retry could not handle, because `git fetch <url> <b>` creates
    neither a local branch nor a remote-tracking ref), and it cannot conflict,
    because there is no merge.
    """
    try:
        branch = normalise_branch(branch)
    except ValueError as e:
        return {"ok": False, "reason": f"invalid branch name: {e}"}
    start = start_point or f"origin/{branch}"
    result = _git(["checkout", "-f", "-B", branch, start], cwd=path, timeout=120)
    if result.returncode != 0:
        return {"ok": False,
                "reason": f"could not check out {branch}: {result.stderr.strip()[:200]}"}
    log(f"Checkout is on {branch}")
    return {"ok": True, "reason": ""}


# ── CLI ───────────────────────────────────────────────────────────────────────
#
# test-authoring-agent's run.sh used to carry its own bash clone-and-sync block,
# which is how it ended up with the two bugs this module exists to not have: a
# `git clone "$_PUSH_URL"` that leaves the token in .git/config for the life of
# the checkout, and a fetch-then-retry that cannot materialise a branch the
# checkout has never seen. Bash calling this is the same shape run.sh already
# uses for `python3 -m shared.metrics`.

def _cli(argv: List[str]) -> int:
    if not argv or argv[0] != "prepare-base":
        print("usage: python3 -m shared.workspace prepare-base [--checkout]")
        return 2

    load_repo_env()
    org = os.environ.get("GITHUB_ORG", "")
    repo = os.environ.get("GITHUB_REPO_AUTOMATION", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    branch = os.environ.get("GITHUB_DEFAULT_BRANCH", "main").strip()
    if not branch:
        # Explicitly blank means "whatever is checked out" — the escape hatch
        # test-authoring-agent's ship step already honours. Agreeing with it
        # here is the point; the two used to disagree, because run.sh defaulted
        # a blank value to "main" and the ship step read it as "current HEAD".
        emit("GITHUB_DEFAULT_BRANCH is empty — leaving the checkout as it is")
        return 0

    path = ensure(os.environ.get("WORKSPACE_DIR", ""), repo, org=org, token=token,
                  branch=branch, log=emit)
    if path is None:
        emit(f"ERROR: automation repo not found and could not be cloned "
             f"({_describe_location()})")
        return 1

    result = prepare_base(path, org, repo, token, branch, log=emit)
    if not result["ok"]:
        emit(f"ERROR: {result['reason']}")
        return 1

    if "--checkout" in argv[1:]:
        moved = checkout_base(path, result["branch"], result["sha"], log=emit)
        if not moved["ok"]:
            emit(f"ERROR: {moved['reason']}")
            return 1
    print(str(path))
    return 0


def _describe_location() -> str:
    override = configured()
    if override is not None:
        return f"FRAMEWORK_DIR={str(override)!r}"
    return (f"{os.environ.get('GITHUB_REPO_AUTOMATION', '<unset>')!r} under "
            f"WORKSPACE_DIR={os.environ.get('WORKSPACE_DIR', '<unset>')!r}")


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
