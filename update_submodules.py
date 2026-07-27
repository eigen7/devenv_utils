#!/usr/bin/env python3
"""Bump each submodule pointer to the latest commit on its GitHub `origin`.

For each submodule this script fetches the submodule's `origin` default branch --
the authoritative "latest from remote", which also picks up commits pushed to
GitHub outside this machine's Gitea flow -- and offers to advance the recorded
pointer to it. It runs on demand, from either side of Docker.

Its contract is narrow: it acts only from a superproject that is fully published
and sitting at its remote head, and it refuses anything else. Before prompting
it verifies the superproject is on `main`, has a clean tree, and has a local
`main` equal to both its Gitea `main` and its GitHub `origin` main; any failure
prints the reason and `Please run \\`git publish\\` and then retry.` and exits
non-zero.

Gitea keeps three roles per submodule, all checked before any prompt:
  * Unpublished-merge guard: if the submodule's Gitea `main` holds commits
    `origin` lacks, a submodule PR was merged locally but not yet published
    (`git publish`'s job), so the submodule is refused. An unreachable Gitea is
    refused too -- an unpublished merge cannot be ruled out.
  * Staging heal: if `origin` is ahead of the submodule's Gitea `main`, the two
    are one repo the machine mirrors, and the lag would make a later
    `publish_submodule` origin push non-fast-forward, so Gitea is fast-forwarded
    to `origin` (a warning, not a failure, if that push cannot be made).
  * Divergence guard: if Gitea `main` and `origin` have each diverged, the
    submodule is refused for manual reconciliation.
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
from .gitea_client import REMOTE_NAME, gitmodule_entries
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
    submodule_gitea_tip,
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


def reject(message: str, remedy: str = REMEDY):
    """Print a rejection -- the tailored reason plus a remedy -- to stderr. The
    remedy defaults to the publish-and-retry sentence; a case publish cannot fix
    (e.g. a Gitea/origin divergence) passes its own."""
    print(f"{message}.\n{remedy}", file=sys.stderr)


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


def origin_tip(sub: Path) -> str | None:
    """The submodule's `origin` default-branch tip, fetched so it is a local
    object. None when `origin` cannot be reached."""
    if git_result(sub, "fetch", "--quiet", "origin").returncode != 0:
        return None
    return git_out(sub, "rev-parse", f"origin/{origin_default_branch(sub)}")


def gitea_vs_origin(sub: Path, gitea: str, origin: str) -> str:
    """How the submodule's Gitea main relates to its origin tip: 'equal',
    'origin_ahead' (origin contains gitea and more), 'gitea_ahead' (gitea has
    commits origin lacks -- an unpublished merge), or 'diverged'."""
    if gitea == origin:
        return "equal"
    if is_ancestor(sub, gitea, origin):
        return "origin_ahead"
    if is_ancestor(sub, origin, gitea):
        return "gitea_ahead"
    return "diverged"


def catch_gitea_up(repo_root: Path, name: str, sub_path: str, origin: str):
    """Fast-forward the submodule's Gitea main to its origin tip, reconciling the
    machine's staging so a later `publish_submodule` origin push stays a
    fast-forward. A failed push is a warning, not a failure -- origin is the
    authority and the bump is still valid."""
    result = git_result(repo_root / sub_path, "push", REMOTE_NAME, f"{origin}:refs/heads/main")
    if result.returncode == 0:
        print(f"{name}: fast-forwarded the submodule's Gitea main to origin ({short(origin)}).")
    else:
        print(
            f"warning: {name}: could not fast-forward the submodule's Gitea main to origin; "
            "continuing.",
            file=sys.stderr,
        )


def bump_explanation(offer: BumpOffer) -> str:
    return (
        f"The {offer.name} submodule's GitHub origin has commits the recorded\n"
        "pointer does not include yet -- the latest published upstream. Answering Y\n"
        "checks the submodule out at that tip and commits the pointer bump on the\n"
        "current branch; run `git publish` on the host to ship it.\n"
        "\n"
        "Proceeding with Y runs the commands:\n"
        "\n"
        f"{bump_commands_text(offer.name, offer.sub_path, offer.tip)}"
    )


def reconcile_gitea(repo_root: Path, name: str, sub_path: str, gitea: str, origin: str) -> str:
    """Act on the Gitea-vs-origin relationship before a bump is considered.
    Returns 'ok' to proceed, or 'rejected' after refusing the submodule."""
    relation = gitea_vs_origin(repo_root / sub_path, gitea, origin)
    if relation == "gitea_ahead":
        reject(f"{name}: Gitea has merged submodule changes that are not pushed to origin yet")
        return "rejected"
    if relation == "diverged":
        reject(
            f"{name}: the submodule's Gitea main and origin have diverged",
            remedy="Reconcile the submodule's Gitea main and origin manually, then retry.",
        )
        return "rejected"
    if relation == "origin_ahead":
        catch_gitea_up(repo_root, name, sub_path, origin)
    return "ok"


def update_one(repo_root: Path, name: str, sub_path: str) -> str:
    """Handle one submodule. Returns 'bumped', 'current', 'skipped' (a warning
    was printed), or 'rejected' (the run must end non-zero)."""
    sub = repo_root / sub_path
    origin = origin_tip(sub)
    if origin is None:
        reject(f"{name}: the submodule's origin could not be reached to find the latest commit")
        return "rejected"
    gitea = submodule_gitea_tip(repo_root, sub_path)
    if gitea is None:
        reject(
            f"{name}: the submodule's Gitea repo could not be reached, so an unpublished merge "
            "cannot be ruled out"
        )
        return "rejected"
    if reconcile_gitea(repo_root, name, sub_path, gitea, origin) == "rejected":
        return "rejected"
    offer = evaluate_bump(repo_root, name, sub_path, origin)
    if offer.status == "none":
        print(f"{name}: up to date")
        return "current"
    if offer.status in ("diverged", "unsafe"):
        print(f"warning: {offer.warning}", file=sys.stderr)
        return "skipped"
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
