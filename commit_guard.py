#!/usr/bin/env python3
"""Keep direct `main` commits in lockstep with Gitea -- the repo's
pre-commit, post-commit, and post-merge hooks (installed by
SetupWizardTool.setup_git_config()).

`main` advances through Gitea: PRs merge there, and `git publish`
fast-forwards the local checkout from it. A commit made directly on the
local `main` is a legitimate shortcut past the PR flow, but on its own it
opens a divergence window -- a Gitea-side merge landing while the commit
sits local-only leaves the two `main`s with a commit each that the other
lacks, which `git publish` refuses. These hooks close the window from both
ends:

  pre-commit (`check`): refuse a commit on `main` while Gitea's `main` has
      merges the local `main` lacks. At that moment the change is still
      uncommitted working-tree state: run `git publish`, then re-commit on
      the caught-up `main`. Allowed with a warning when Gitea is
      unreachable; bypass deliberately with `git commit --no-verify`.

  post-commit / post-merge (`sync`): immediately mirror the new `main` tip
      to the `gitea` remote, so Gitea is never behind for longer than a
      commit takes. Rewriting `main` -- `git commit --amend` on a tip that
      was already mirrored, most of all -- mirrors too, by replacing the
      superseded Gitea tip. A push that fails for any other reason cannot
      un-commit; it prints what happened and the way to reconcile.

Both actions no-op anywhere that isn't the `main` branch of a checkout with
a `gitea` remote -- feature worktrees, detached HEADs, and submodule
checkouts are untouched. The push targets the local Gitea service only;
nothing here ever touches GitHub (that is `git publish`, guarded by
prepush_guard.py).
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (submodules/devenv_utils/commit_guard.py):
    # put the repo root on sys.path and adopt the package identity so the
    # relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import subprocess

from .config import DevenvConfig, load_config
from .gitea_client import REMOTE_NAME
from .publish import gitea_tip_superseded, main_relationship

UNPUBLISHED_MERGES_MESSAGE = (
    "Gitea's main has merged commits your local main lacks, so this commit would\n"
    "make the two histories diverge. Your change is safe (still uncommitted):\n"
    "run `git publish` on the host to catch main up, then commit again.\n"
    "To bypass deliberately: git commit --no-verify"
)

SYNC_FAILED_MESSAGE = (
    "WARNING: could not mirror main to Gitea (`git push gitea main` failed).\n"
    "Local main and Gitea's main can now diverge. Run `git publish` on the host:\n"
    "it reports what each side holds and offers the reconciliation that fits."
)


def current_branch(repo_root: Path) -> str | None:
    """The checked-out branch name, or None on a detached HEAD."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def has_gitea_remote(repo_root: Path) -> bool:
    return (
        subprocess.run(
            ["git", "remote", "get-url", REMOTE_NAME], capture_output=True, cwd=repo_root
        ).returncode
        == 0
    )


def guards_main(repo_root: Path) -> bool:
    """Whether the hooks apply here: the `main` branch of a checkout with a
    `gitea` remote. False everywhere else (feature worktrees, detached HEADs,
    checkouts that never registered with the service)."""
    return current_branch(repo_root) == "main" and has_gitea_remote(repo_root)


def gitea_main_sha(repo_root: Path) -> str | None:
    """The tip of the `gitea` remote's main, or None when it cannot be read
    (service unreachable, or no server-side repo/branch yet)."""
    result = subprocess.run(
        ["git", "ls-remote", REMOTE_NAME, "main"], capture_output=True, text=True, cwd=repo_root
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def check(repo_root: Path):
    """The pre-commit action: block the commit while Gitea's main is ahead."""
    if not guards_main(repo_root):
        return
    sha = gitea_main_sha(repo_root)
    if sha is None:
        print(
            "warning: could not reach Gitea to check for unpublished merges; allowing commit.",
            file=sys.stderr,
        )
        return
    if main_relationship(repo_root, sha) in ("behind", "diverged"):
        sys.exit(UNPUBLISHED_MERGES_MESSAGE)


def remote_tracking_sha(repo_root: Path, ref: str) -> str | None:
    """The commit a remote-tracking ref names, or None when the checkout has no
    such ref. Read locally: no network, so the answer is as fresh as the last
    fetch."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    return result.stdout.strip() or None


def push_main(repo_root: Path, replacing: str | None = None):
    """Push the local main to the Gitea remote, replacing the named tip when one
    is given.

    --recurse-submodules=no: this is a same-machine mirror of `main` to the
    local Gitea service, and every submodule commit `main` references came from
    a Gitea merge, so it is already on Gitea. push.recurseSubmodules=check
    exists to protect the GitHub publishing invariant (never push a superproject
    commit whose submodule commit GitHub lacks); it would otherwise abort this
    mirror whenever a just-recorded submodule commit sits in the submodule clone
    only as a fetched object, in no remote-tracking ref."""
    lease = [f"--force-with-lease=main:{replacing}"] if replacing else []
    return subprocess.run(
        ["git", "push", "--quiet", "--recurse-submodules=no", *lease, REMOTE_NAME, "main"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )


def superseded_gitea_tip(repo_root: Path) -> str | None:
    """Gitea's main tip, when replacing it with the local main would discard
    nothing but a copy of rewritten local history (see
    publish.gitea_tip_superseded). None when Gitea is unreachable or its tip
    holds anything worth keeping."""
    sha = gitea_main_sha(repo_root)
    if sha is None:
        return None
    origin_tip = remote_tracking_sha(repo_root, "origin/main")
    return sha if gitea_tip_superseded(repo_root, sha, origin_tip) else None


def sync(repo_root: Path):
    """The post-commit/post-merge action: mirror the new main tip to Gitea,
    replacing a tip this checkout has rewritten.

    Never fails the hook -- the commit already exists; a failed push only
    reports how to reconcile.
    """
    if not guards_main(repo_root):
        return
    result = push_main(repo_root)
    if result.returncode == 0:
        print("mirrored main -> gitea")
        return
    superseded = superseded_gitea_tip(repo_root)
    if superseded is None:
        print(f"{result.stderr.strip()}\n{SYNC_FAILED_MESSAGE}", file=sys.stderr)
        return
    # The lease makes this safe against the seconds between reading the tip and
    # replacing it: a PR merged into Gitea in that window fails the push rather
    # than being discarded.
    forced = push_main(repo_root, replacing=superseded)
    if forced.returncode == 0:
        print(f"mirrored rewritten main -> gitea (replaced {superseded[:9]})")
    else:
        print(f"{forced.stderr.strip()}\n{SYNC_FAILED_MESSAGE}", file=sys.stderr)


ACTIONS = {
    "pre-commit": check,
    "post-commit": sync,
    "post-merge": sync,
}


def main(cfg: DevenvConfig):
    ACTIONS[sys.argv[1]](cfg.repo_root)


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
