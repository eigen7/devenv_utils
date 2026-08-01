#!/usr/bin/env python3
"""Block commits that edit a vendored subtree -- installed as a pre-commit hook.

subtrees/ holds read-only vendored copies of repos we control (see
SUBTREES.md): they change only by `git subtree pull`, never by direct edits. A
subtree pull itself passes untouched -- git does not run pre-commit for merge
commits -- so this hook only ever fires on the accident it exists to catch: an
edit under subtrees/ that belongs in the source repo's own working clone.
`git commit --no-verify` bypasses deliberately (e.g. to conclude a conflicted
subtree merge by hand).
"""

import subprocess
import sys

SUBTREES_PREFIX = "subtrees/"


def main():
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    # Only paths *inside* a vendored copy (subtrees/<name>/...) are protected;
    # the files directly under subtrees/ (the package marker, the README
    # pointer) are project-owned.
    hits = sorted(
        path for path in staged if path.startswith(SUBTREES_PREFIX) and path.count("/") >= 2
    )
    if not hits:
        return
    listing = "\n".join(f"  {path}" for path in hits)
    sys.exit(
        f"Commit blocked: staged changes inside a read-only vendored copy under {SUBTREES_PREFIX}:\n"
        f"{listing}\n"
        "Author the change in the source repo's working clone under the project mount\n"
        "and land it through a PR there; this repo then picks it up with\n"
        "`git subtree pull` (see subtrees/devenv_utils/SUBTREES.md).\n"
        "`git commit --no-verify` bypasses deliberately."
    )


if __name__ == "__main__":
    main()
