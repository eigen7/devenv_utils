#!/usr/bin/env python3
"""Guard `git push` to GitHub origin -- installed as the repo's pre-push hook.

Publishing to origin goes through `git publish` on the host, which also
fast-forwards the local checkout and cleans up merged worktrees. A bare
`git push` to origin is caught here in the two ways it goes wrong:

  - run inside the container, where the origin credentials don't live, or
  - run while the local `main` and Gitea's `main` disagree. Gitea ahead means
    an unpublished browser-merge: a bare push would be a silent no-op (the
    merge commit isn't local yet), stranding the merge on Gitea. Local ahead
    means a commit made directly on `main` that bypassed Gitea: pushing it to
    origin would leave Gitea behind the published history.

Either way the hook stops the push and prints the way out, matched to the
direction of the mismatch. Pushes to any other remote -- notably Gitea, the
normal in-container path -- pass through untouched. git invokes the hook with
the remote name as argv[1] and its URL as argv[2].
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import subprocess

from .config import DevenvConfig, load_config
from .gitea_client import SERVICE_CONTAINER
from .publish import gitea_read_url, main_relationship
from .state import in_docker_container

LOCAL_AHEAD_ADVICE = (
    "Local main has commits that Gitea's main lacks. Run `git publish` -- it\n"
    "syncs Gitea's main automatically before publishing."
)

DIVERGED_ADVICE = (
    "Local main and Gitea's main have diverged: each has commits the other lacks.\n"
    "Run `git publish` -- it works out the safe reconciliation (merge vs rebase),\n"
    "asks before acting, and then publishes."
)


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
        # Fail closed: pushing to GitHub around an unreachable Gitea is how a
        # commit lands on origin that Gitea has never seen -- the diverged
        # state that cannot be rebased away afterwards.
        sys.exit(
            "Could not reach Gitea, so there is no way to check for unpublished\n"
            "merges; this push to GitHub origin is blocked. Start the service --\n"
            f"`docker start {SERVICE_CONTAINER}` on the host -- and retry, or push\n"
            "anyway, deliberately, with `git push --no-verify`."
        )
    gitea_main = probe.stdout.split()[0] if probe.stdout.strip() else ""
    if not gitea_main:
        return
    relation = main_relationship(root, gitea_main)
    if relation == "behind":
        sys.exit(
            "Gitea's main has merged commits your local main doesn't have yet.\n"
            "Run `git publish` -- it fast-forwards, publishes to origin, and cleans up."
        )
    if relation == "ahead":
        sys.exit(LOCAL_AHEAD_ADVICE)
    if relation == "diverged":
        sys.exit(DIVERGED_ADVICE)


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
