#!/usr/bin/env python3
"""Pull every vendored subtree up to its upstream main.

git subtree records nothing about where a subtree came from, so the raw pull
needs the prefix, URL, branch, and --squash spelled out every time. This wraps
the routine case: each subtrees/<name>/ of the invoking repo is pulled from
https://github.com/<owner>/<name>.git main, where <owner> comes from the
repo's origin remote. Each pull lands as a squash + merge commit on the
current branch; review and push as usual.

For the non-routine case -- pulling from a working clone's branch to test a
coordinated change -- use the raw command (see SUBTREES.md).
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (as a CLI tool): load the package
    # under its canonical name from this file's own directory, whatever that
    # directory is called -- subtrees/devenv_utils/ in a consumer repo, the
    # repo root (or a worktree of it, named after its branch) in
    # devenv_utils' own working clone.
    import importlib.util

    _pkg_dir = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location(
        "devenv_utils",
        _pkg_dir / "__init__.py",
        submodule_search_locations=[str(_pkg_dir)],
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["devenv_utils"] = _pkg
    _spec.loader.exec_module(_pkg)
    __package__ = "devenv_utils"

import subprocess

from .console import SetupException
from .github_access import origin_repo

SUBTREES_DIR = "subtrees"


def main():
    toplevel = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    # Refuse to pull onto a branch that is behind origin: the pull's push
    # would be rejected, forcing a reconciliation with subtree commits in
    # flight (rebase_guard.py polices the dangerous form of that). Pulling
    # from a current branch avoids the whole situation.
    subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=toplevel, check=True)
    behind = (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"], cwd=toplevel
        ).returncode
        != 0
    )
    if behind:
        sys.exit("This branch is behind origin/main; run `git pull --no-rebase`, then rerun.")
    # A vendored subtree is a *committed* directory under subtrees/, so
    # enumerate the git tree, not the filesystem -- which also holds junk like
    # the __pycache__ of subtrees/__init__.py.
    entries = subprocess.run(
        ["git", "ls-tree", "HEAD", f"{SUBTREES_DIR}/"],
        capture_output=True,
        text=True,
        check=True,
        cwd=toplevel,
    ).stdout.splitlines()
    # ls-tree lines: "<mode> <type> <sha>\t<path>"; the subtrees are the tree entries.
    fields = [line.split(None, 3) for line in entries]
    names = sorted(f[3].split("/")[-1] for f in fields if f[1] == "tree")
    if not names:
        sys.exit(f"No vendored subtrees under {toplevel / SUBTREES_DIR}; nothing to pull.")
    owner = origin_repo(toplevel).split("/")[0]
    for name in names:
        prefix = f"{SUBTREES_DIR}/{name}"
        url = f"https://github.com/{owner}/{name}.git"
        print(f"Pulling {prefix} from {url} ...")
        subprocess.run(
            ["git", "subtree", "pull", "--prefix", prefix, url, "main", "--squash"],
            cwd=toplevel,
            check=True,
        )


if __name__ == "__main__":
    try:
        main()
    except SetupException as e:
        for arg in e.args:
            print(arg, file=sys.stderr)
        sys.exit(1)
