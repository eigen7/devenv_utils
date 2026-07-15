"""Enumerate a repo's git worktrees and identify its primary checkout.

All worktrees of a repository share one common git dir -- the primary
checkout's .git -- so the primary checkout is resolvable from inside any
worktree. Two callers depend on getting this right:

* The PR workflow (pr_flow.py) must run its git operations -- fast-forwarding
  main, sourcing submodule commits and the setup stamp, locating a feature
  worktree to remove -- against the *primary* checkout, never whichever
  worktree's shim happened to invoke the tool.
* The stale-worktree report (stale_worktrees.py) lists every *secondary*
  worktree, i.e. every worktree that is not the primary checkout.

Both derive "which worktree is primary" the same way, so it lives here once.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorktreeEntry:
    path: Path
    branch: str | None  # None when the worktree is on a detached HEAD


def _git(anchor: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=True, cwd=anchor)
    return result.stdout


def primary_worktree(anchor: Path) -> Path:
    """The primary checkout's working directory, resolved from any worktree.

    The primary checkout owns the repo's one real git dir; every worktree's
    `.git` file points into it. `git rev-parse --git-common-dir` yields that
    shared dir, whose parent is the primary checkout -- the same answer whether
    `anchor` is the primary checkout itself or a feature worktree. Callers pass
    their own checkout as `anchor` and get back the primary regardless.
    """
    common = _git(anchor, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return Path(common).parent


def list_worktrees(anchor: Path) -> list[WorktreeEntry]:
    """Every worktree of the repo (git lists the primary checkout first)."""
    entries = []
    path, branch = None, None
    for line in _git(anchor, "worktree", "list", "--porcelain").splitlines() + [""]:
        if line.startswith("worktree "):
            path, branch = Path(line.removeprefix("worktree ")), None
        elif line.startswith("branch refs/heads/"):
            branch = line.removeprefix("branch refs/heads/")
        elif not line and path is not None:
            entries.append(WorktreeEntry(path, branch))
            path = None
    return entries


def secondary_worktrees(anchor: Path) -> list[WorktreeEntry]:
    """Every worktree except the primary checkout."""
    primary = primary_worktree(anchor)
    return [w for w in list_worktrees(anchor) if w.path != primary]


def worktree_for_branch(anchor: Path, branch: str) -> Path | None:
    """The secondary worktree that has `branch` checked out, if any."""
    for worktree in secondary_worktrees(anchor):
        if worktree.branch == branch:
            return worktree.path
    return None
