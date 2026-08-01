#!/usr/bin/env python3
"""Block rebases that would replay subtree commits -- the repo's pre-rebase hook.

A subtree pull lands as a squash commit whose tree is rooted at the *source*
repo, tied into place by a merge commit. Rebase linearizes the merge away and
replays the squash commit as an ordinary patch, spilling source-repo files
into this repo's root. So a rebase whose replayed range contains subtree
commits -- identified by the git-subtree-dir trailer git subtree stamps into
them -- is refused; reconcile with a merge (`git pull --no-rebase`) instead.
`git rebase --no-verify` bypasses deliberately.

git invokes the hook with the upstream as argv[1] and, when rebasing a branch
other than the current one, that branch as argv[2].
"""

import subprocess
import sys


def main():
    upstream = sys.argv[1]
    branch = sys.argv[2] if len(sys.argv) > 2 else "HEAD"
    marked = subprocess.run(
        ["git", "rev-list", "--grep", "git-subtree-dir:", f"{upstream}..{branch}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if marked:
        listing = "\n".join(f"  {sha[:12]}" for sha in marked)
        sys.exit(
            "Rebase blocked: it would replay git-subtree commits, whose squash trees\n"
            "are rooted at the source repo -- replayed as ordinary patches they spill\n"
            "source-repo files into this repo's root:\n"
            f"{listing}\n"
            "Reconcile with a merge instead (`git pull --no-rebase` / `git merge`).\n"
            "`git rebase --no-verify` bypasses deliberately."
        )


if __name__ == "__main__":
    main()
