#!/usr/bin/env python3
"""Keep in-container pushes off origin's main -- the repo's pre-push hook.

The container's GitHub token exists so coding agents can push feature branches
and open PRs; `main` advances only by merging a PR, or by the user from the
host. This hook enforces that split: a push from inside the container that
would update origin's `main` -- including deleting it -- is blocked, while
host pushes and container pushes to other branches pass untouched. It is the
client-side stand-in for branch protection, which private repos on GitHub's
free plan don't get. `git push --no-verify` bypasses deliberately.

git invokes the hook with the remote name as argv[1] and its URL as argv[2],
and feeds the pushed ref updates on stdin as
`<local ref> <local sha> <remote ref> <remote sha>` lines.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (as git does): put the package's parent
    # directory on sys.path and adopt the package identity so the relative
    # imports resolve. Works at any nesting -- subtrees/devenv_utils/ in a
    # consumer repo, the repo root in devenv_utils' own clone.
    _package_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(_package_dir.parent))
    __package__ = _package_dir.name

from .state import in_docker_container

PROTECTED_REF = "refs/heads/main"


def is_origin_push(remote_name: str, remote_url: str) -> bool:
    return remote_name == "origin" or "github.com" in remote_url


def main():
    remote_name = sys.argv[1] if len(sys.argv) > 1 else ""
    remote_url = sys.argv[2] if len(sys.argv) > 2 else ""
    if not is_origin_push(remote_name, remote_url) or not in_docker_container():
        return
    updates = [line.split() for line in sys.stdin.read().splitlines()]
    if any(update[2:3] == [PROTECTED_REF] for update in updates):
        sys.exit(
            f"Push blocked: {PROTECTED_REF} is not updated from inside the container.\n"
            "Land the change through a pull request (pr_flow.py create), or push from\n"
            "the host. `git push --no-verify` bypasses deliberately."
        )


if __name__ == "__main__":
    main()
