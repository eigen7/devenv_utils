"""Report git worktrees with no recent activity, so abandoned ones get cleaned up.

The PR workflow (pr_flow.py) creates a worktree per task. A task abandoned
mid-flight (chat closed, container relaunched) leaves its worktree behind with
nobody responsible for it. Nothing deletes worktrees automatically -- one may
hold uncommitted work -- so this tool only reports: every worktree (other than
the main checkout) whose last activity is older than a threshold is listed
with enough context (branch, uncommitted files, merged-into-main) for the
user to decide what to delete.

"Last activity" is the newest of: the checked-out tip's commit time, the
worktree index's mtime (staging), and the mtimes of files git reports as
changed or untracked (unstaged edits).

Consumer repos expose this through a thin py/tools/stale_worktrees.py shim;
gitea_serve.py also prints the same report every time it runs.
"""

import argparse
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import DevenvConfig
from .worktrees import WorktreeEntry, secondary_worktrees

DEFAULT_STALE_DAYS = 7


@dataclass
class StaleWorktree:
    worktree: WorktreeEntry
    last_activity: float  # unix time
    changed_files: int  # uncommitted (staged, unstaged, or untracked) files
    merged: bool  # tip is an ancestor of main


def git(worktree_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(worktree_path), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def changed_files(worktree_path: Path) -> list[Path]:
    """Absolute paths of files with uncommitted (including untracked) changes.

    --no-optional-locks keeps the query from refreshing the index, whose mtime
    is part of the activity signal: a plain `git status` would bump it on
    every report run, so no worktree could ever look idle.
    """
    lines = git(worktree_path, "--no-optional-locks", "status", "--porcelain").splitlines()
    return [worktree_path / line[3:] for line in lines]


def last_activity(worktree_path: Path, changed: list[Path]) -> float:
    """Unix time of the newest sign of life in the worktree."""
    times = [float(git(worktree_path, "log", "-1", "--format=%ct"))]
    git_dir = Path(git(worktree_path, "rev-parse", "--absolute-git-dir").strip())
    for path in [git_dir / "index"] + changed:
        if path.exists():
            times.append(path.stat().st_mtime)
    return max(times)


def is_merged(worktree_path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "merge-base", "--is-ancestor", "HEAD", "main"],
        capture_output=True,
    )
    return result.returncode == 0


def find_stale_worktrees(repo_root: Path, stale_days: float) -> list[StaleWorktree]:
    cutoff = time.time() - stale_days * 86400
    stale = []
    for worktree in secondary_worktrees(repo_root):
        changed = changed_files(worktree.path)
        activity = last_activity(worktree.path, changed)
        if activity < cutoff:
            stale.append(StaleWorktree(worktree, activity, len(changed), is_merged(worktree.path)))
    return sorted(stale, key=lambda s: s.last_activity)


def describe(stale: StaleWorktree) -> str:
    age_days = int((time.time() - stale.last_activity) / 86400)
    branch = stale.worktree.branch
    if stale.changed_files:
        state = f"{stale.changed_files} uncommitted file(s)"
    elif stale.merged:
        state = "clean, merged into main -- safe to delete"
    else:
        state = "clean, NOT merged into main"
    # A branch-backed worktree is torn down (worktree + local branch, even if
    # unmerged) via the PR workflow tool. A detached-HEAD worktree has no
    # branch to name, so fall back to raw git; --force because git refuses to
    # remove a worktree whose submodules are populated -- the normal state
    # here, and the state line above says what --force would discard.
    if branch is not None:
        removal = f"pr.py abandon {branch}"
    else:
        removal = f"git worktree remove --force {stale.worktree.path}"
    return (
        f"  {stale.worktree.path}\n"
        f"    branch {branch or '(detached HEAD)'}; last activity {age_days} day(s) ago; {state}\n"
        f"    delete with: {removal}"
    )


def print_stale_report(cfg: DevenvConfig, stale_days: float = DEFAULT_STALE_DAYS):
    """Print abandoned-worktree candidates, or nothing if there are none.

    Called from gitea_serve.py on every run; a session seeing this output
    should surface it to the user rather than delete anything itself.
    """
    stale = find_stale_worktrees(cfg.repo_root, stale_days)
    if not stale:
        return
    print(f"Worktrees with no activity in {stale_days:g}+ days (candidates for manual deletion):")
    for entry in stale:
        print(describe(entry))


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--days",
        type=float,
        default=DEFAULT_STALE_DAYS,
        help="report worktrees with no activity in this many days (default: %(default)s)",
    )
    return parser.parse_args()


def main(cfg: DevenvConfig):
    args = get_args()
    if not find_stale_worktrees(cfg.repo_root, args.days):
        print(f"No worktrees idle for {args.days:g}+ days.")
        return
    print_stale_report(cfg, args.days)
