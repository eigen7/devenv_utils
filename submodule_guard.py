#!/usr/bin/env python3
"""Submodule pointer safety and working-tree sync -- part of the repo's
pre-commit hook, plus the post-checkout/post-merge sync action (installed by
SetupWizardTool.setup_git_config()).

Stock git leaves two gaps in the submodule model SUBMODULES.md describes;
each action here closes one:

  pre-commit (`pre-commit`): refuse a commit that moves a submodule pointer
      *backward*. The typical cause is a stale submodule checkout swept into
      the index by a broad `git add`: the older commit already exists
      upstream, so `push.recurseSubmodules=check` cannot catch it, and the
      rewind lands looking deliberate. The fix is to sync the checkout and
      re-stage; a genuinely intended rewind goes through with
      `git commit --no-verify`.

  sync (`sync`): update each populated submodule working tree to the commit
      the superproject records. `submodule.recurse=true` covers checkout and
      pull, but `git rebase` -- fast-forward or not -- leaves submodule
      working trees stale; running this from post-checkout and post-merge
      closes that gap. Sync never discards work: a submodule with
      uncommitted changes, or with commits the recorded pointer lacks, is
      left alone with a warning. Unpopulated submodules are also left alone
      -- populating a fresh worktree is pr_flow.py's job (it clones from the
      main checkout, covering pointers whose commit is not upstream yet),
      and setup_common self-heals fresh clones.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (submodules/devenv_utils/submodule_guard.py):
    # put the repo root on sys.path and adopt the package identity so the
    # relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import subprocess

from .config import DevenvConfig, load_config

GITLINK_MODE = "160000"

REWIND_MESSAGE = """\
This commit would move the submodule {path} backward:
{new} is an ancestor of the currently recorded {old}.
That usually means the submodule checkout is stale and a broad `git add`
staged it. To sync the checkout and re-stage the pointer:
    git submodule update --init {path}
    git add {path}
To rewind deliberately: git commit --no-verify"""


def git_result(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def warn(message: str):
    print(f"warning: {message}", file=sys.stderr)


def is_ancestor(repo: Path, maybe_ancestor: str, of: str) -> bool | None:
    """Whether `maybe_ancestor` is an ancestor of `of` in `repo`, or None when
    git cannot answer (e.g. one of the commits is absent from the checkout)."""
    rc = git_result(repo, "merge-base", "--is-ancestor", maybe_ancestor, of).returncode
    return {0: True, 1: False}.get(rc)


def staged_pointer_moves(repo_root: Path) -> list[tuple[str, str, str]]:
    """The staged submodule pointer changes, as (path, old_sha, new_sha).

    Pointer additions and submodule removals change the entry's mode away
    from gitlink-on-both-sides and are excluded: only a move between two
    commits can be a rewind.
    """
    moves = []
    for line in git_out(repo_root, "diff", "--cached", "--raw", "--no-renames").splitlines():
        meta, path = line.split("\t", 1)
        old_mode, new_mode, old_sha, new_sha, _status = meta.lstrip(":").split()
        if old_mode == GITLINK_MODE and new_mode == GITLINK_MODE:
            moves.append((path, old_sha, new_sha))
    return moves


def check(repo_root: Path):
    """The pre-commit action: block staged backward submodule pointer moves."""
    for path, old_sha, new_sha in staged_pointer_moves(repo_root):
        rewind = is_ancestor(repo_root / path, new_sha, old_sha)
        if rewind is None:
            warn(f"could not determine whether {path} moves backward; allowing commit.")
        elif rewind:
            sys.exit(REWIND_MESSAGE.format(path=path, old=old_sha[:7], new=new_sha[:7]))


def recorded_pointers(repo_root: Path) -> list[tuple[str, str]]:
    """Every submodule the index records, as (path, recorded_sha)."""
    pointers = []
    for line in git_out(repo_root, "ls-files", "-s").splitlines():
        meta, path = line.split("\t", 1)
        mode, sha, _stage = meta.split()
        if mode == GITLINK_MODE:
            pointers.append((path, sha))
    return pointers


def checked_out_head(submodule: Path) -> str | None:
    """The submodule checkout's HEAD, or None when it is not populated."""
    result = git_result(submodule, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def has_uncommitted_changes(submodule: Path) -> bool:
    return bool(git_out(submodule, "status", "--porcelain", "--untracked-files=no").strip())


def sync_one(repo_root: Path, path: str, recorded: str):
    """Bring one stale submodule checkout to the recorded pointer, or explain
    why it was left alone."""
    submodule = repo_root / path
    if has_uncommitted_changes(submodule):
        warn(f"{path} has uncommitted changes; leaving it at its current commit.")
        return
    if is_ancestor(submodule, checked_out_head(submodule), recorded) is not True:
        warn(f"{path} has commits the recorded pointer lacks; leaving it as checked out.")
        return
    result = git_result(repo_root, "submodule", "update", "--", path)
    if result.returncode == 0:
        print(f"synced {path} -> {recorded[:7]}")
    else:
        warn(f"could not sync {path}: {result.stderr.strip()}")


def sync(repo_root: Path):
    """The post-checkout/post-merge action: sync stale submodule checkouts."""
    for path, recorded in recorded_pointers(repo_root):
        head = checked_out_head(repo_root / path)
        if head is not None and head != recorded:
            sync_one(repo_root, path, recorded)


ACTIONS = {
    "pre-commit": check,
    "sync": sync,
}


def main(cfg: DevenvConfig):
    ACTIONS[sys.argv[1]](cfg.repo_root)


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
