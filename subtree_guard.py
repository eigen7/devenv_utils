#!/usr/bin/env python3
"""Enforce that vendored subtrees under subtrees/<dir>/ stay read-only.

A subtree is a mirror of an upstream repo; it may change only via
`git subtree pull` (a merge commit, which git exempts from the pre-commit hook).
Any ordinary commit that touches subtrees/<dir>/ is rejected. To change a
subtree, edit its upstream repo and run ./py/tools/pull_git_subtrees.py.

This is vendored in devenv_utils so every consuming repo shares one
implementation: hooks/pre-commit calls it with --staged, and each consumer's
`subtree-readonly` CI job calls it with a commit range.

Usage:
    subtree_guard.py --staged           # check the staged index (pre-commit)
    subtree_guard.py <base> <head>      # check commits in <base>..<head> (CI)
    subtree_guard.py <range>            # e.g. origin/main..HEAD
"""
import re
import subprocess
import sys

_SUBTREE_RE = re.compile(r"^subtrees/([^/]+)/")
_SELF = "subtrees/devenv_utils/subtree_guard.py"
_RULE = "=" * 72


def _git(args: list) -> list:
    out = subprocess.run(["git", *args], capture_output=True, text=True,
                         check=True).stdout
    return [line for line in out.splitlines() if line]


def _subtree_paths(paths: list) -> list:
    return [p for p in paths if _SUBTREE_RE.match(p)]


def _err(*lines) -> None:
    for line in lines:
        print(line, file=sys.stderr)


def _check_staged() -> int:
    offending = _subtree_paths(
        _git(["diff", "--cached", "--name-only", "--diff-filter=ACMRD"]))
    if not offending:
        return 0
    _err("", _RULE,
         "Your commit was blocked by the pre-commit hook.", "",
         "Files under subtrees/<dir>/ are a READ-ONLY mirror of an upstream repo",
         "and can't be committed directly:", "")
    for path in offending:
        _err(f"    {path}")
    _err("",
         "To change a vendored subtree, edit its own upstream repo, then run:",
         "    ./py/tools/pull_git_subtrees.py", "",
         "To unstage these (your file edits are kept):  git reset",
         "To bypass for ONE commit (only if you are SURE): git commit --no-verify",
         _RULE)
    return 1


def _check_range(rng: str) -> int:
    # Walk first-parent history only: a `git subtree pull` lands as a merge plus
    # a squash commit on the merge's second-parent side, so following first
    # parents (and dropping merges) skips both and leaves the mainline commits a
    # developer actually authored.
    bad = []
    for sha in _git(["rev-list", "--first-parent", "--no-merges", rng]):
        offending = _subtree_paths(
            _git(["diff-tree", "--no-commit-id", "--name-only", "-r", sha]))
        if offending:
            bad.append((sha, offending))

    if not bad:
        print(f"subtree-readonly: OK -- no commit in {rng} edits a vendored subtree.")
        return 0

    _err("", _RULE, "subtree-readonly: these commits edit a read-only vendored subtree",
         f"  (check: {_SELF})", "")
    for sha, offending in bad:
        _err(f"  commit {sha[:12]}")
        for path in offending:
            _err(f"      {path}")
    _err("", "Subtrees may change only via `git subtree pull`. Edit the upstream",
         "repo instead, or drop these changes.", _RULE)
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
