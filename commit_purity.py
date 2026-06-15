#!/usr/bin/env python3
"""Enforce that each commit touches exactly one "unit".

A unit is a single vendored subtree (subtrees/<dir>/...) or the parent repo
(everything else). A commit may not mix the parent with a subtree, nor two
different subtrees -- so every commit stays pushable to a single destination.
Merge commits (e.g. a `git subtree pull`) are exempt.

This lives in devenv_utils so any consuming repo can share it: the pre-commit
hook (hooks/pre-commit) calls it with --staged, and a CI job calls it with a
commit range.

Usage:
    commit_purity.py --staged            # check the staged index (pre-commit)
    commit_purity.py <base> <head>       # check commits in <base>..<head> (CI)
    commit_purity.py <range>             # e.g. origin/main..HEAD
"""
import re
import subprocess
import sys

# Convention shared by the devenv_utils tooling: vendored subtrees live one
# level under "subtrees/", so subtrees/<name>/ is the unit named <name>. Files
# directly under subtrees/ (README.md, __init__.py) are part of the parent.
_SUBTREE_RE = re.compile(r"^subtrees/([^/]+)/")

# This script's path within a consuming repo, used to self-identify in messages.
_SELF = "subtrees/devenv_utils/commit_purity.py"
_RULE = "=" * 72


def _unit(path: str) -> str:
    """Return the unit a path belongs to: 'subtree:<name>' or 'parent'."""
    match = _SUBTREE_RE.match(path)
    return f"subtree:{match.group(1)}" if match else "parent"


def _units(paths: list) -> dict:
    """Map each unit touched by *paths* to its files."""
    by_unit = {}
    for path in paths:
        by_unit.setdefault(_unit(path), []).append(path)
    return by_unit


def _git(args: list) -> list:
    """Run a git command and return its non-empty output lines."""
    out = subprocess.run(["git", *args], capture_output=True, text=True,
                         check=True).stdout
    return [line for line in out.splitlines() if line]


def _err(*lines) -> None:
    for line in lines:
        print(line, file=sys.stderr)


def _report(units: dict) -> None:
    for unit, files in sorted(units.items()):
        _err(f"    [{unit}]")
        for path in files[:5]:
            _err(f"      {path}")
        if len(files) > 5:
            _err(f"      ... and {len(files) - 5} more")


def _check_staged() -> int:
    staged = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMRD"])
    units = _units(staged)
    if len(units) <= 1:
        return 0

    _err("", _RULE)
    _err("Your commit was blocked by the pre-commit hook")
    _err(f"  (check: {_SELF})")
    _err("")
    _err("In this repo, a commit must touch only ONE unit -- a single subtree, or the parent")
    _err("repo -- but these staged changes span several:")
    _err("")
    _report(units)
    _err("")
    _err("Fix: stage and commit each unit separately.")
    if staged:
        _err("")
        _err("To unstage everything first (your file edits are kept):")
        _err("    git reset")
    _err("")
    _err("To bypass this check for ONE commit -- only if you are SURE you want")
    _err("a mixed commit:")
    _err("    git commit --no-verify")
    _err(_RULE)
    return 1


def _check_range(rng: str) -> int:
    bad = []
    for sha in _git(["rev-list", "--no-merges", rng]):
        units = _units(_git(["diff-tree", "--no-commit-id", "--name-only", "-r", sha]))
        if len(units) > 1:
            bad.append((sha, units))

    if not bad:
        print(f"commit-purity: OK -- every commit in {rng} touches a single unit.")
        return 0

    _err("", _RULE)
    _err("commit-purity: these commits each mix more than one unit")
    _err(f"  (check: {_SELF})")
    _err("")
    for sha, units in bad:
        _err(f"  commit {sha[:12]}")
        _report(units)
    _err("")
    _err("Each commit must touch a single unit -- a single subtree, or the")
    _err("parent repo. Split these commits (e.g. with `git rebase -i`) so each")
    _err("touches only one unit.")
    _err(_RULE)
    return 1


def main(argv: list) -> int:
    if argv[1:] == ["--staged"]:
        return _check_staged()
    if len(argv) == 2:
        return _check_range(argv[1])
    if len(argv) == 3:
        return _check_range(f"{argv[1]}..{argv[2]}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
