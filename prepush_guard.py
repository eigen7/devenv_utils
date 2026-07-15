#!/usr/bin/env python3
"""Guard `git push` to GitHub origin -- installed as the repo's pre-push hook.

Publishing to origin goes through `git publish` on the host, which also
fast-forwards the local checkout and cleans up merged worktrees. A bare
`git push` to origin is caught here in the two ways it goes wrong:

  - run inside the container, where the origin credentials don't live, or
  - run while Gitea's `main` is ahead of the local `main` -- a browser-merge
    that hasn't been published. A bare push would be a silent no-op (the merge
    commit isn't local yet), stranding the merge on Gitea.

Either way the hook stops the push and points at `git publish`. Pushes to any
other remote -- notably Gitea, the normal in-container path -- pass through
untouched. git invokes the hook with the remote name as argv[1] and its URL as
argv[2].
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import subprocess

from .config import DevenvConfig, load_config
from .publish import gitea_read_url
from .state import in_docker_container


def is_origin_push(remote_name: str, remote_url: str) -> bool:
    return remote_name == "origin" or "github.com" in remote_url


def main(cfg: DevenvConfig):
    remote_name = sys.argv[1] if len(sys.argv) > 1 else ""
    remote_url = sys.argv[2] if len(sys.argv) > 2 else ""
    if not is_origin_push(remote_name, remote_url):
        return
    if in_docker_container():
        sys.exit(
            "Push to GitHub origin from the HOST, via `git publish` -- not the container "
            "(the origin credentials live on the host)."
        )
    root = cfg.repo_root
    probe = subprocess.run(
        ["git", "ls-remote", gitea_read_url(root), "main"], cwd=root, capture_output=True, text=True
    )
    if probe.returncode != 0:
        print(
            "warning: could not reach Gitea to check for unpublished merges; allowing push.",
            file=sys.stderr,
        )
        return
    gitea_main = probe.stdout.split()[0] if probe.stdout.strip() else ""
    local_main = subprocess.run(
        ["git", "rev-parse", "main"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    if gitea_main and gitea_main != local_main:
        sys.exit(
            "Gitea's main has merged commits your local main doesn't have yet.\n"
            "Run `git publish` -- it fast-forwards, publishes to origin, and cleans up."
        )


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
