#!/usr/bin/env python3
"""Merge an approved pull request on the local Gitea instance from the terminal.

The command-line equivalent of clicking "Merge" on the Gitea web page -- the
"accept" step, kept separate from publishing to GitHub (that is `git publish`,
run on the host; see publish.py). Merging in the browser is the normal path;
this is the terminal alternative.

    submodules/devenv_utils/gitea_merge.py <repo> <pr-number>

<repo> is the Gitea repo to merge in -- the consumer itself, or one of its
submodules, which get their own PR when a change spans both. Naming the repo
explicitly is deliberate: a coordinated change has a PR #N in each repo, and
the tool must never guess which one you mean.

Runs inside the dev container, which is wired up to the Gitea service and
its admin credentials (see gitea_client.py). Accepting a PR only advances
Gitea's `main`; nothing reaches your local checkout or GitHub until you run
`git publish` on the host.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (submodules/devenv_utils/gitea_merge.py):
    # put the repo root on sys.path and adopt the package identity so the
    # relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import argparse

from .config import DevenvConfig, load_config
from .gitea_client import container_access
from .state import in_docker_container


def merge_pr(cfg: DevenvConfig, repo: str, number: int):
    if not in_docker_container():
        raise SystemExit(
            "gitea_merge runs inside the dev container, where the Gitea admin "
            "credentials are mounted. Merge from the container (or just use the "
            "Gitea web page)."
        )
    access = container_access()
    access.ensure_reachable()
    admin = access.admin_creds()
    owner = admin["username"]
    pr = access.api("GET", f"/repos/{owner}/{repo}/pulls/{number}", admin)
    if pr["merged"]:
        print(f"{owner}/{repo} PR #{number} is already merged.")
        return
    if not pr["mergeable"]:
        raise SystemExit(f"{owner}/{repo} PR #{number} is not mergeable (conflicts or checks).")
    access.api("POST", f"/repos/{owner}/{repo}/pulls/{number}/merge", admin, {"Do": "merge"})
    print(f"Merged {owner}/{repo} PR #{number}. Publish it with `git publish` on the host.")


def main(cfg: DevenvConfig):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("repo", help="Gitea repo to merge in (the consumer or a submodule)")
    parser.add_argument("number", type=int, help="pull request number in that repo")
    args = parser.parse_args()
    merge_pr(cfg, args.repo, args.number)


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
