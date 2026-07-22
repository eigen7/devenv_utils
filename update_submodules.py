#!/usr/bin/env python3
"""Bump each submodule pointer to a submodule commit already published upstream.

The shared Gitea/GitHub devenv_utils repos mean a submodule commit another
consumer project published reaches this project's Gitea `main` too, ahead of the
recorded pointer. This script offers to advance the pointer to such a commit,
on demand, from either side of Docker.

Its contract is narrow: it acts only from a superproject that is fully published
and sitting at its remote head, and it refuses anything else. Before prompting
it verifies the superproject is on `main`, has a clean tree, and has a local
`main` equal to both its Gitea `main` and its GitHub `origin` main; any failure
prints the reason and `Please run \\`git publish\\` and then retry.` and exits
non-zero. Per submodule, a Gitea tip ahead of the pointer is offered only when
that tip has reached the submodule's GitHub `origin` -- a tip that is merged on
Gitea but not yet on `origin` is `git publish`'s job, so it is refused rather
than offered. An unreachable `origin`, on either level, is refused too: this
script exists to verify the published state, so it fails closed.
"""

import subprocess
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
from .publish import confirm, main_relationship, origin_default_branch, print_commits
from .submodule_bump import (
    BumpOffer,
    bump_commands_text,
    bump_commit,
    bump_header,
    bump_question,
    evaluate_bump,
    git_out,
    git_result,
    gitea_read_url,
    has_uncommitted_changes,
    is_ancestor,
    short,
)

REMEDY = "Please run `git publish` and then retry."

CLOSING_MESSAGE = "\nPlease run `git publish` on the host to push the change to remote."

# main_relationship results paired with why each blocks the run, per remote.
_GITEA_REASONS = {
    "ahead": "the local main has commits not yet on Gitea",
    "behind": "Gitea has merges the local main lacks",
    "diverged": "the local main has diverged from Gitea",
    "unreachable": "Gitea could not be reached to verify the published state",
}
_ORIGIN_REASONS = {
    "ahead": "the local main has commits not yet on GitHub origin",
    "behind": "GitHub origin main is ahead of the local main",
    "diverged": "the local main has diverged from GitHub origin",
    "unreachable": "the published state could not be verified: GitHub origin could not be reached",
}


def reject(message: str):
    """Print a rejection -- the tailored reason plus the uniform remedy -- to
    stderr."""
    print(f"{message}.\n{REMEDY}", file=sys.stderr)


def fetched_relationship(repo_root: Path, target: str) -> str:
    """How the local `main` relates to `target`'s main (a remote name or URL):
    the main_relationship verdict, or 'unreachable' when the fetch fails."""
    if git_result(repo_root, "fetch", "--quiet", target, "main").returncode != 0:
        return "unreachable"
    return main_relationship(repo_root, git_out(repo_root, "rev-parse", "FETCH_HEAD"))


def precondition_failure(repo_root: Path) -> str:
    """Why the superproject is not in the fully-published state this script
    requires, or '' when it is: on `main`, clean, and level with both its Gitea
    and its GitHub `origin` main."""
    branch = git_out(repo_root, "branch", "--show-current")
    if branch != "main":
        return f"the superproject is not on branch main (on {branch or 'a detached HEAD'})"
    if has_uncommitted_changes(repo_root):
        return "the superproject has uncommitted changes"
    try:
        gitea_url = gitea_read_url(repo_root)
    except subprocess.CalledProcessError:
        return "no gitea remote is configured to verify the published state"
    gitea = fetched_relationship(repo_root, gitea_url)
    if gitea != "equal":
        return _GITEA_REASONS[gitea]
    origin = fetched_relationship(repo_root, "origin")
    if origin != "equal":
        return _ORIGIN_REASONS[origin]
    return ""


def tip_published(sub: Path, tip: str) -> bool | None:
    """Whether `tip` has reached the submodule's GitHub `origin` default branch.
    None when `origin` cannot be reached."""
    if git_result(sub, "fetch", "--quiet", "origin").returncode != 0:
        return None
    return is_ancestor(sub, tip, f"origin/{origin_default_branch(sub)}")


def bump_explanation(offer: BumpOffer) -> str:
    return (
        f"The {offer.name} submodule's Gitea main has commits the recorded pointer\n"
        "does not include yet, already published to the submodule's GitHub origin\n"
        "(for a shared submodule, typically a change another consumer project\n"
        "published). Answering Y checks the submodule out at that tip and commits\n"
        "the pointer bump on the current branch; run `git publish` on the host to\n"
        "ship it.\n"
        "\n"
        "Proceeding with Y runs the commands:\n"
        "\n"
        f"{bump_commands_text(offer.name, offer.sub_path, offer.tip)}"
    )


def update_one(repo_root: Path, name: str, sub_path: str) -> str:
    """Handle one submodule. Returns 'bumped', 'current', 'skipped' (a warning
    was printed), or 'rejected' (the run must end non-zero)."""
    offer = evaluate_bump(repo_root, name, sub_path)
    if offer is None:
        reject(f"{name}: the submodule's Gitea repo could not be reached")
        return "rejected"
    if offer.status == "none":
        print(f"{name}: up to date")
        return "current"
    if offer.status in ("diverged", "unsafe"):
        print(f"warning: {offer.warning}", file=sys.stderr)
        return "skipped"
    published = tip_published(repo_root / sub_path, offer.tip)
    if published is None:
        reject(f"{name}: the submodule's origin could not be reached to verify the update")
        return "rejected"
    if not published:
        reject(f"{name}: Gitea has merged submodule changes that are not pushed to origin yet")
        return "rejected"
    print_commits(bump_header(offer), list(offer.spanned))
    if not confirm(bump_question(offer), bump_explanation(offer)):
        return "skipped"
    bump_commit(offer, repo_root)
    print(f"  committed pointer bump: {sub_path} -> {short(offer.tip)}")
    return "bumped"


def update_submodules(repo_root: Path) -> int:
    """Verify the published-state preconditions, then offer a published pointer
    bump per submodule. Returns the process exit code: non-zero when the run was
    rejected up front or any submodule was refused."""
    reason = precondition_failure(repo_root)
    if reason:
        reject(f"Cannot update submodules: {reason}")
        return 1
    bumped = rejected = False
    for name, sub_path in gitmodule_entries(repo_root):
        result = update_one(repo_root, name, sub_path)
        bumped = bumped or result == "bumped"
        rejected = rejected or result == "rejected"
    if bumped:
        print(CLOSING_MESSAGE)
    return 1 if rejected else 0


def main(cfg: DevenvConfig):
    sys.exit(update_submodules(cfg.repo_root))


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
