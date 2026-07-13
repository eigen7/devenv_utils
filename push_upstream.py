#!/usr/bin/env python3
"""Push submodule commits upstream, then hand back the superproject push.

Run from the superproject root **on the host** (where upstream credentials
live):

    python3 submodules/devenv_utils/push_upstream.py

For every submodule recorded in HEAD, pushes the recorded pointer commit to
the submodule's origin default branch if it isn't already reachable there,
then prints the follow-up command that publishes the superproject. This
preserves the ordering invariant from SUBMODULES.md: a superproject commit
must never be published before the submodule commits it references, or other
clones could not fetch them.

The pushes are plain fast-forwards: if a submodule's upstream branch has
moved past the local commit's history, the push fails and the divergence has
to be reconciled by hand (merge or rebase inside the submodule, plus a
pointer bump).

Standalone stdlib-only script: the host has no project environment to import.
"""

import subprocess
import sys
from pathlib import Path


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check, cwd=cwd)


def submodule_paths(superproject: Path) -> list[Path]:
    listing = git(
        superproject, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"
    )
    return [Path(line.split(" ", 1)[1]) for line in listing.stdout.splitlines()]


def recorded_pointer(superproject: Path, sub_path: Path) -> str:
    """The submodule commit recorded in the superproject's HEAD."""
    entry = git(superproject, "ls-tree", "HEAD", str(sub_path)).stdout.split()
    return entry[2]


def upstream_branch(sub: Path) -> str:
    """The submodule origin's default branch (falls back to main when the
    local clone never learned origin/HEAD)."""
    result = git(sub, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if result.returncode != 0:
        return "main"
    return result.stdout.strip().removeprefix("origin/")


def is_upstream(sub: Path, sha: str, branch: str) -> bool:
    result = git(sub, "merge-base", "--is-ancestor", sha, f"origin/{branch}", check=False)
    return result.returncode == 0


def push_pointer(sub: Path, sha: str, branch: str):
    print(f"  pushing {sha[:12]} -> origin/{branch} ...")
    result = git(sub, "push", "origin", f"{sha}:refs/heads/{branch}", check=False)
    if result.returncode != 0:
        sys.exit(
            f"push failed for {sub}:\n{result.stderr}\n"
            "If upstream has diverged, reconcile inside the submodule (merge or "
            "rebase onto origin) and bump the superproject pointer, then re-run."
        )


def superproject_branch(superproject: Path) -> str:
    return git(superproject, "branch", "--show-current").stdout.strip() or "main"


def main():
    superproject = Path.cwd()
    if not (superproject / ".gitmodules").exists():
        sys.exit("No .gitmodules here; run from the superproject root.")

    for sub_path in submodule_paths(superproject):
        sub = superproject / sub_path
        sha = recorded_pointer(superproject, sub_path)
        git(sub, "fetch", "origin")
        branch = upstream_branch(sub)
        if is_upstream(sub, sha, branch):
            print(f"{sub_path}: pointer {sha[:12]} already on origin/{branch}.")
        else:
            print(f"{sub_path}: pointer {sha[:12]} not on origin/{branch}.")
            push_pointer(sub, sha, branch)

    print(
        "\nSubmodules are up to date upstream. Publish the superproject with:\n\n"
        f"    git push origin {superproject_branch(superproject)}\n"
    )


if __name__ == "__main__":
    main()
