#!/usr/bin/env python3
"""Offer to advance each submodule's recorded pointer to its Gitea `main` tip.

A submodule PR merges on its own, leaving the superproject's recorded pointer
naming the pre-merge commit until a superproject commit bumps it. `git publish`
offers that bump as part of publishing; this script offers it on demand, from
either side of Docker, without publishing anything.

For each submodule it reports whether the pointer is current, and when the
submodule's Gitea `main` is ahead it lists the new commits and asks whether to
commit the bump on the current branch. A committed bump is mirrored to Gitea by
the post-commit hook; run `git publish` on the host to push it to GitHub.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (submodules/devenv_utils/update_submodules.py):
    # put the repo root on sys.path and adopt the package identity so the
    # relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

from .config import DevenvConfig, load_config
from .gitea_client import gitmodule_entries
from .publish import confirm, print_commits
from .submodule_bump import (
    BumpOffer,
    bump_commands_text,
    bump_commit,
    bump_header,
    bump_question,
    evaluate_bump,
    short,
)

CLOSING_MESSAGE = "\nPlease run `git publish` on the host to push the change to remote."


def bump_explanation(offer: BumpOffer) -> str:
    return (
        f"The {offer.name} submodule's Gitea main has commits the recorded pointer\n"
        "does not include yet -- typically a submodule PR that just merged. Answering\n"
        "Y checks the submodule out at that tip and commits the pointer bump on the\n"
        "current branch; run `git publish` on the host to ship it.\n"
        "\n"
        "Proceeding with Y runs the commands:\n"
        "\n"
        f"{bump_commands_text(offer.name, offer.sub_path, offer.tip)}"
    )


def update_one(repo_root: Path, name: str, sub_path: str) -> bool:
    """Offer to bump one submodule's pointer. Returns whether a bump was
    committed. Prints a status line for a current pointer (this is an explicit
    invocation, so silence would be wrong) and a warning for one that cannot be
    reached or bumped safely.

    A None result means the freshness check never ran -- no Gitea URL could be
    derived, or the fetch failed (e.g. the service is down). Unlike a hook,
    which fails open silently, this script exists to answer "is anything newer
    upstream?", so it reports the outage rather than claiming the pointer is
    current."""
    offer = evaluate_bump(repo_root, name, sub_path)
    if offer is None:
        print(
            f"warning: {name}: could not reach the submodule's Gitea repo; skipped.",
            file=sys.stderr,
        )
        return False
    if offer.status == "none":
        print(f"{name}: up to date")
        return False
    if offer.status in ("diverged", "unsafe"):
        print(f"warning: {offer.warning}", file=sys.stderr)
        return False
    print_commits(bump_header(offer), list(offer.spanned))
    if not confirm(bump_question(offer), bump_explanation(offer)):
        return False
    bump_commit(offer, repo_root)
    print(f"  committed pointer bump: {sub_path} -> {short(offer.tip)}")
    return True


def update_submodules(repo_root: Path):
    """Offer a pointer bump for each submodule, then point at `git publish` if
    anything was committed."""
    bumped = False
    for name, sub_path in gitmodule_entries(repo_root):
        if update_one(repo_root, name, sub_path):
            bumped = True
    if bumped:
        print(CLOSING_MESSAGE)


def main(cfg: DevenvConfig):
    update_submodules(cfg.repo_root)


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
