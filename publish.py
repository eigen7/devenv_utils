#!/usr/bin/env python3
"""Publish accepted Gitea merges to GitHub, from the host. Backs `git publish`.

After a PR is merged on Gitea (the browser "Merge" button, or gitea_merge.py),
the merge lives only on the Gitea server. This command -- run on the **host**,
where the GitHub credentials live -- catches everything up in one shot:

  1. fast-forward the local `main` to Gitea's `main` (fetched read-only over the
     nginx web port, no auth: the repos are public),
  2. check out each submodule to its newly recorded pointer, fetching the commit
     from Gitea when the local clone lacks it,
  3. push each submodule's pointer commit to its GitHub `origin`, then the
     superproject to its `origin` (submodule-first, so `push.recurseSubmodules`
     is satisfied),
  4. tear down every worktree whose branch is now merged into `main`.

It publishes whatever Gitea's `main` currently holds -- not one specific PR --
because `main` is linear: a later merge sits on top of earlier ones, so `origin`
can only be caught up to the tip. Idempotent: re-run after a partial failure.

Accepting a PR (`gitea_merge.py` / the web UI) happens in the container; only
this step needs the host. The pre-push hook redirects a stray `git push` here.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (submodules/devenv_utils/publish.py):
    # put the repo root on sys.path and adopt the package identity so the
    # relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import shutil
import subprocess
import urllib.parse

from .config import DevenvConfig, load_config
from .pr_flow import commit_present, gitmodule_entries, submodule_pointer
from .state import in_docker_container
from .worktrees import secondary_worktrees


def git(cwd: Path, *args: str):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def gitea_read_url(repo_root: Path, sub_path: str = "") -> str:
    """The auth-free web-port URL of a Gitea repo, for read-only fetch.

    Everything is derived from the parent's `gitea` remote -- always present, as
    the review remote. That remote targets the loopback backend port with
    embedded admin credentials; on the host only the nginx web port (backend - 1)
    is reachable, and the repos are public, so drop the credentials and step the
    port down. A submodule's Gitea repo lives under the same owner, named after
    its GitHub origin (the same project), so it needs no `gitea` remote of its
    own -- which a fresh submodule clone lacks.
    """
    parent = urllib.parse.urlparse(git_out(repo_root, "remote", "get-url", "gitea"))
    base = f"http://{parent.hostname}:{parent.port - 1}"
    if not sub_path:
        return f"{base}{parent.path}"
    owner = parent.path.strip("/").split("/")[0]
    origin = urllib.parse.urlparse(git_out(repo_root / sub_path, "remote", "get-url", "origin"))
    name = Path(origin.path).stem
    return f"{base}/{owner}/{name}.git"


def is_ancestor(repo: Path, maybe_ancestor: str, of: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", maybe_ancestor, of], cwd=repo
        ).returncode
        == 0
    )


LOCAL_AHEAD_ADVICE = (
    "Local main has commits that Gitea's main lacks. main advances through Gitea,\n"
    "so sync it first: `git push gitea main`, then run `git publish`."
)

DIVERGED_ADVICE = (
    "Local main and Gitea's main have diverged: each has commits the other lacks.\n"
    "Reconcile them by hand, then run `git publish`."
)


def main_relationship(repo_root: Path, gitea_main: str) -> str:
    """How the local `main` relates to Gitea's `main` tip: 'equal', 'behind'
    (Gitea has commits local main lacks), 'ahead' (local main has commits Gitea
    lacks), or 'diverged'. A tip commit absent from the local repo counts as
    'behind': the local branch cannot contain a commit it has never seen."""
    if gitea_main == git_out(repo_root, "rev-parse", "main"):
        return "equal"
    if not commit_present(repo_root, gitea_main) or is_ancestor(repo_root, "main", gitea_main):
        return "behind"
    if is_ancestor(repo_root, gitea_main, "main"):
        return "ahead"
    return "diverged"


def fast_forward_main(repo_root: Path):
    """Fast-forward the local `main` to Gitea's `main`.

    Publishing flows Gitea -> local -> GitHub, so a local `main` that is ahead
    of or diverged from Gitea's cannot be fast-forwarded; refuse with the way
    to reconcile instead of letting `git merge`/`git push` fail obscurely."""
    if git_out(repo_root, "branch", "--show-current") != "main":
        raise SystemExit("git publish must run on `main`; check it out first.")
    git(repo_root, "fetch", gitea_read_url(repo_root), "main")
    relation = main_relationship(repo_root, git_out(repo_root, "rev-parse", "FETCH_HEAD"))
    if relation == "ahead":
        raise SystemExit(LOCAL_AHEAD_ADVICE)
    if relation == "diverged":
        raise SystemExit(DIVERGED_ADVICE)
    git(repo_root, "merge", "--ff-only", "FETCH_HEAD")


def sync_submodule(repo_root: Path, sub_path: str):
    """Check out the submodule to its recorded pointer, fetching from Gitea when
    the pointer commit isn't local yet (its GitHub push happens next)."""
    sub = repo_root / sub_path
    pointer = submodule_pointer(repo_root, sub_path)
    if not commit_present(sub, pointer):
        git(sub, "fetch", gitea_read_url(repo_root, sub_path))
    git(repo_root, "submodule", "update", "--init", sub_path)


def origin_default_branch(sub: Path) -> str:
    """The submodule origin's default branch (falls back to main when the clone
    never learned origin/HEAD)."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=sub,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "main"
    return result.stdout.strip().removeprefix("origin/")


def publish_submodule(repo_root: Path, sub_path: str):
    """Push the submodule's recorded pointer to its GitHub origin if missing."""
    sub = repo_root / sub_path
    pointer = submodule_pointer(repo_root, sub_path)
    git(sub, "fetch", "origin")
    branch = origin_default_branch(sub)
    if is_ancestor(sub, pointer, f"origin/{branch}"):
        print(f"  {sub_path}: {pointer[:12]} already on origin/{branch}")
    else:
        git(sub, "push", "origin", f"{pointer}:refs/heads/{branch}")
        print(f"  {sub_path}: pushed {pointer[:12]} -> origin/{branch}")


def teardown_merged_worktrees(repo_root: Path):
    """Remove every worktree whose branch is now merged into `main`.

    Done host-side with rm + prune rather than `git worktree remove`, which
    chokes on the container-absolute gitdir pointers baked into the worktree.
    """
    for worktree in secondary_worktrees(repo_root):
        if worktree.branch is None or not is_ancestor(repo_root, worktree.branch, "main"):
            continue
        shutil.rmtree(worktree.path, ignore_errors=True)
        git(repo_root, "worktree", "prune")
        git(repo_root, "branch", "-d", worktree.branch)
        print(f"  removed merged worktree {worktree.path} ({worktree.branch})")


def publish(repo_root: Path):
    if in_docker_container():
        raise SystemExit(
            "git publish runs on the HOST, where the GitHub credentials live -- not in "
            "the container. Accept PRs in the container/browser; publish from the host."
        )
    print("Fast-forwarding local main from Gitea...")
    fast_forward_main(repo_root)
    entries = gitmodule_entries(repo_root)
    for _, sub_path in entries:
        sync_submodule(repo_root, sub_path)
    print("Publishing to GitHub origin (submodules first)...")
    for _, sub_path in entries:
        publish_submodule(repo_root, sub_path)
    git(repo_root, "push", "origin", "main")
    print("  superproject pushed -> origin/main")
    print("Cleaning up merged worktrees...")
    teardown_merged_worktrees(repo_root)
    print("Published.")


def main(cfg: DevenvConfig):
    publish(cfg.repo_root)


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
