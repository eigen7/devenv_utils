#!/usr/bin/env python3
"""Publish accepted Gitea merges to GitHub, from the host. Backs `git publish`.

After a PR is merged on Gitea (the browser "Merge" button, or gitea_merge.py),
the merge lives only on the Gitea server. This command -- run on the **host**,
where the GitHub credentials live -- catches everything up in one shot:

  1. reconcile the local `main` with Gitea's `main` (fetched read-only over the
     nginx web port, no auth: the repos are public) and with GitHub's. The
     normal case is a plain fast-forward; a diverged history, or commits that
     reached GitHub outside the Gitea flow, are resolved interactively --
     merge vs rebase chosen so that nothing another repository already holds
     is ever rewritten (see sync_main),
  2. check out each submodule to its newly recorded pointer, fetching the commit
     from Gitea when the local clone lacks it,
  3. push each submodule's pointer commit to its GitHub `origin`, then the
     superproject to its `origin` (submodule-first, so `push.recurseSubmodules`
     is satisfied),
  4. tear down every worktree whose branch is now merged into `main`.

It publishes whatever Gitea's `main` currently holds -- not one specific PR --
because `main` is linear: a later merge sits on top of earlier ones, so `origin`
can only be caught up to the tip. Idempotent: re-run after a partial failure.

Accepting a PR (`gitea_merge.py` / the web UI) happens in the container; only
this step needs the host. The pre-push hook redirects a stray `git push` here.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (submodules/devenv_utils/publish.py):
    # put the repo root on sys.path and adopt the package identity so the
    # relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import shutil
import subprocess
import sys

from .config import DevenvConfig, load_config
from .gitea_client import REMOTE_NAME, gitmodule_entries
from .pr_flow import commit_present, submodule_pointer
from .state import in_docker_container
from .submodule_bump import (
    BumpOffer,
    bump_commands_text,
    bump_commit,
    bump_header,
    bump_question,
    evaluate_bump,
    gitea_read_url,
    is_ancestor,
    short,
)
from .worktrees import secondary_worktrees


def git(cwd: Path, *args: str):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


DECLINED_NOTICE = "Nothing was changed or published."

MERGE_GITEA_EXPLANATION = (
    "These local commits are already on GitHub -- most likely pushed to GitHub\n"
    "directly and pulled into this checkout -- while Gitea separately picked up\n"
    "new merges. Commits on GitHub can never be rewritten, so the two histories\n"
    "are joined with a merge.\n"
    "\n"
    "Proceeding with Y runs the command:\n"
    "\n"
    '    git merge -m "Merge gitea main" {tip}'
)

REBASE_EXPLANATION = (
    "Local main has commits that Gitea is missing. This likely happened either\n"
    "because you made manual commits, or because you merged Gitea commits from\n"
    "a concurrent agent session. None of them are on GitHub yet, so they can be\n"
    "replayed on top of Gitea's main, keeping history linear.\n"
    "\n"
    "Proceeding with Y runs the command:\n"
    "\n"
    "    git rebase {tip}"
)

REPLACE_GITEA_EXPLANATION = (
    "Gitea's main tip is a commit this checkout made and has since rewritten --\n"
    "the state a `git commit --amend` on main leaves behind. GitHub does not have\n"
    "it, and nothing else can: Gitea's main only ever advances by a PR merge or\n"
    "by this checkout's mirror. Replacing it discards the superseded copy.\n"
    "\n"
    "Rebasing instead would replay your commit onto its own older self, so every\n"
    "hunk it touches conflicts.\n"
    "\n"
    "Proceeding with Y runs the command:\n"
    "\n"
    "    git push --force-with-lease=main:{tip} gitea main"
)

PUBLISHED_REWRITE_NOTICE = (
    "Note: Gitea's tip is a commit you rewrote locally, but GitHub already has\n"
    "it, so it cannot be discarded. Reconciling replays your version on top of\n"
    "it, which conflicts wherever the two touch the same lines."
)

MERGE_GITHUB_EXPLANATION = (
    "GitHub has commits that never went through Gitea -- most likely someone\n"
    "pushed to GitHub directly.\n"
    "\n"
    "Proceeding with Y runs the command:\n"
    "\n"
    '    git merge -m "Merge GitHub origin main" {tip}'
)


def confirm(question: str, explanation: str) -> bool:
    """Interactive yes/no prompt, defaulting to yes; `?` prints the
    explanation and asks again."""
    while True:
        answer = input(f"{question} [Y/n/?] ").strip().lower()
        if answer == "?":
            print(explanation)
        else:
            return answer not in ("n", "no")


def print_commits(header: str, lines: list):
    print(header)
    for line in lines:
        print(f"  {line}")


def commits_beyond(repo: Path, tip: str, *excludes: str) -> list:
    """Commits reachable from `tip` but from none of `excludes`, newest first,
    as `<short-hash> <subject>` display lines."""
    out = git_out(repo, "log", "--format=%h %s", tip, *[f"^{e}" for e in excludes])
    return out.splitlines() if out else []


def merge_or_abort(repo: Path, tip: str, label: str):
    """Merge `tip` into main; on conflicts, restore the checkout and bounce
    the conflict resolution to the user."""
    try:
        git(repo, "merge", "-m", f"Merge {label}", tip)
    except subprocess.CalledProcessError:
        subprocess.run(["git", "merge", "--abort"], cwd=repo)
        raise SystemExit(
            f"The merge of {label} hit conflicts. It was aborted -- your checkout\n"
            f"is unchanged. Run `git merge {tip[:12]}`, resolve the conflicts,\n"
            "then re-run `git publish`."
        ) from None


def rebase_or_abort(repo: Path, tip: str):
    """Rebase main onto `tip`; on conflicts, restore the checkout and bounce
    the conflict resolution to the user."""
    try:
        git(repo, "rebase", tip)
    except subprocess.CalledProcessError:
        subprocess.run(["git", "rebase", "--abort"], cwd=repo)
        raise SystemExit(
            "The rebase onto Gitea's main hit conflicts. It was aborted -- your\n"
            f"checkout is unchanged. Run `git rebase {tip[:12]}`, resolve the\n"
            "conflicts, then re-run `git publish`."
        ) from None


def main_reflog_shas(repo_root: Path) -> set:
    """Every commit `main` has ever pointed at in this checkout."""
    return set(git_out(repo_root, "reflog", "main", "--format=%H").split())


def gitea_tip_rewritten(repo_root: Path, gitea_tip: str) -> bool:
    """Whether Gitea's main tip is a commit this checkout made and has since
    rewritten -- what a `git commit --amend` on `main` leaves behind once the
    pre-amend commit has been mirrored. The tip was a local `main` tip once, so
    everything reachable from it was in this checkout and Gitea holds no work of
    its own there."""
    return commit_present(repo_root, gitea_tip) and gitea_tip in main_reflog_shas(repo_root)


def gitea_tip_superseded(repo_root: Path, gitea_tip: str, origin_tip: str | None) -> bool:
    """Whether replacing Gitea's main tip with the local `main` would discard
    nothing: this checkout rewrote the tip, and GitHub does not have it, so no
    published history is rewritten.

    An `origin_tip` of None means GitHub holds nothing this checkout knows of,
    which satisfies the second condition -- at worst GitHub turns out to have
    the commit after all, and the next `git publish` offers to merge it back."""
    if origin_tip is not None and is_ancestor(repo_root, gitea_tip, origin_tip):
        return False
    return gitea_tip_rewritten(repo_root, gitea_tip)


def replace_gitea_main(repo_root: Path, gitea_tip: str, superseded: list) -> bool:
    """Confirm discarding Gitea's superseded main tip. The local `main` already
    holds the rewritten history, so there is nothing to do here beyond agreeing
    that the force-push at the end of sync_main may happen."""
    print_commits("Gitea's main holds commits your main has rewritten:", superseded)
    if not confirm(
        "Replace Gitea's main with yours?", REPLACE_GITEA_EXPLANATION.format(tip=gitea_tip[:12])
    ):
        raise SystemExit(DECLINED_NOTICE)
    return True


def reconcile_diverged_gitea(repo_root: Path, gitea_tip: str, origin_tip: str) -> bool:
    """Reconcile a local `main` that has diverged from Gitea's. Returns whether
    Gitea's main must be force-updated to match the result.

    The recipe follows one rule: never rewrite a commit another repository
    already has. Gitea's own tip is exempt when this checkout is the repository
    that wrote it and has since rewritten it -- then the tip is a stale copy of
    local history and is simply replaced. Otherwise, when some local-only commit
    is already on GitHub origin, a rebase would mint new hashes for published
    history and the final fast-forward push to origin would be rejected -- so
    merge. When every local-only commit is still private, rebasing onto Gitea's
    main rewrites nothing anyone else holds and keeps `main` linear."""
    local_only = commits_beyond(repo_root, "main", gitea_tip)
    gitea_only = commits_beyond(repo_root, gitea_tip, "main")
    if gitea_tip_superseded(repo_root, gitea_tip, origin_tip):
        return replace_gitea_main(repo_root, gitea_tip, gitea_only)
    print_commits("Gitea's main has commits your main lacks:", gitea_only)
    print_commits("Your main has commits Gitea lacks:", local_only)
    if gitea_tip_rewritten(repo_root, gitea_tip):
        print(PUBLISHED_REWRITE_NOTICE)
    private = set(commits_beyond(repo_root, "main", gitea_tip, origin_tip))
    published = [line for line in local_only if line not in private]
    if published:
        print_commits("Of yours, these are already on GitHub:", published)
        explanation = MERGE_GITEA_EXPLANATION.format(tip=gitea_tip[:12])
        if not confirm("Merge Gitea's main into yours?", explanation):
            raise SystemExit(DECLINED_NOTICE)
        merge_or_abort(repo_root, gitea_tip, "gitea main")
    else:
        explanation = REBASE_EXPLANATION.format(tip=gitea_tip[:12])
        if not confirm("Rebase yours onto Gitea's main?", explanation):
            raise SystemExit(DECLINED_NOTICE)
        rebase_or_abort(repo_root, gitea_tip)
    return False


def merge_github_only_commits(repo_root: Path, origin_tip: str):
    """Fold in commits that reached GitHub origin outside the Gitea flow."""
    github_only = commits_beyond(repo_root, origin_tip, "main")
    if not github_only:
        return
    print_commits(
        "The following commits are on GitHub but are missing from Gitea and your main:",
        github_only,
    )
    explanation = MERGE_GITHUB_EXPLANATION.format(tip=origin_tip[:12])
    if not confirm("Merge them into your main?", explanation):
        raise SystemExit(DECLINED_NOTICE)
    merge_or_abort(repo_root, origin_tip, "GitHub origin main")


def main_relationship(repo_root: Path, gitea_main: str) -> str:
    """How the local `main` relates to Gitea's `main` tip: 'equal', 'behind'
    (Gitea has commits local main lacks), 'ahead' (local main has commits Gitea
    lacks), or 'diverged'. A tip commit absent from the local repo counts as
    'behind': the local branch cannot contain a commit it has never seen."""
    if gitea_main == git_out(repo_root, "rev-parse", "main"):
        return "equal"
    if not commit_present(repo_root, gitea_main) or is_ancestor(repo_root, "main", gitea_main):
        return "behind"
    if is_ancestor(repo_root, gitea_main, "main"):
        return "ahead"
    return "diverged"


def sync_main(repo_root: Path):
    """Bring the local `main` into agreement with Gitea's and GitHub's.

    Publishing flows Gitea -> local -> GitHub, so the normal case fast-forwards
    the local `main` to Gitea's tip. Two abnormal states are reconciled
    interactively: a local `main` that diverged from Gitea's (see
    reconcile_diverged_gitea for the merge-vs-rebase choice), and commits that
    reached GitHub outside the Gitea flow (merge is the only option for those:
    they can never be rewritten). The rebase runs before the GitHub merge so
    private commits are linearized first. Afterwards Gitea is brought up to
    the reconciled `main` -- which also covers a `main` that was simply ahead
    (a direct commit whose commit_guard mirror push didn't land) -- so the
    GitHub pushes that follow are guaranteed fast-forwards. That last push is
    forced only to discard a superseded Gitea tip, under a lease that fails if
    Gitea has moved since it was read."""
    if git_out(repo_root, "branch", "--show-current") != "main":
        raise SystemExit("git publish must run on `main`; check it out first.")
    git(repo_root, "fetch", gitea_read_url(repo_root), "main")
    gitea_tip = git_out(repo_root, "rev-parse", "FETCH_HEAD")
    git(repo_root, "fetch", "origin", "main")
    origin_tip = git_out(repo_root, "rev-parse", "FETCH_HEAD")
    relation = main_relationship(repo_root, gitea_tip)
    force_gitea = False
    if relation == "behind":
        git(repo_root, "merge", "--ff-only", gitea_tip)
    elif relation == "diverged":
        force_gitea = reconcile_diverged_gitea(repo_root, gitea_tip, origin_tip)
    merge_github_only_commits(repo_root, origin_tip)
    if git_out(repo_root, "rev-parse", "main") != gitea_tip:
        print("Syncing Gitea's main to the local main...")
        lease = [f"--force-with-lease=main:{gitea_tip}"] if force_gitea else []
        # --recurse-submodules=no: this mirrors `main` to the same-machine Gitea
        # service, whose submodule commits all came from Gitea merges. The
        # submodule origin pushes happen later in publish (publish_submodule), so
        # a referenced submodule commit can still be a fetched object in no
        # remote-tracking ref here; push.recurseSubmodules=check -- which guards
        # the GitHub publishing invariant, not this local mirror -- would
        # otherwise abort it.
        git(repo_root, "push", "--recurse-submodules=no", *lease, REMOTE_NAME, "main")


def sync_submodule(repo_root: Path, sub_path: str):
    """Check out the submodule to its recorded pointer, fetching from Gitea when
    the pointer commit isn't local yet (its GitHub push happens next)."""
    sub = repo_root / sub_path
    pointer = submodule_pointer(repo_root, sub_path)
    if not commit_present(sub, pointer):
        git(sub, "fetch", gitea_read_url(repo_root, sub_path))
    git(repo_root, "submodule", "update", "--init", sub_path)


def publish_bump_explanation(offer: BumpOffer) -> str:
    return (
        f"The {offer.name} submodule's Gitea main has commits the superproject's\n"
        "recorded pointer does not include yet -- typically a submodule PR that just\n"
        "merged. Answering Y checks the submodule out at that tip and commits the\n"
        "pointer bump on main; the superproject push later in this publish run ships\n"
        "it.\n"
        "\n"
        "Proceeding with Y runs the commands:\n"
        "\n"
        f"{bump_commands_text(offer.name, offer.sub_path, offer.tip)}"
    )


def offer_pointer_bump(repo_root: Path, name: str, sub_path: str):
    """Offer to advance one submodule's recorded pointer to its Gitea main tip.

    Declining continues the publish run (unlike the sync_main prompts, which
    abort): the bump is a convenience, not a precondition. Accepting commits the
    bump on main, which the later `git push origin main` then ships -- after the
    submodule push, so push.recurseSubmodules is satisfied.
    """
    offer = evaluate_bump(repo_root, name, sub_path)
    if offer is None or offer.status == "none":
        return
    if offer.status in ("diverged", "unsafe"):
        print(f"warning: {offer.warning}", file=sys.stderr)
        return
    print_commits(bump_header(offer), list(offer.spanned))
    if confirm(bump_question(offer), publish_bump_explanation(offer)):
        bump_commit(offer, repo_root)
        print(f"  committed pointer bump: {sub_path} -> {short(offer.tip)}")


def origin_default_branch(sub: Path) -> str:
    """The submodule origin's default branch (falls back to main when the clone
    never learned origin/HEAD)."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=sub,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "main"
    return result.stdout.strip().removeprefix("origin/")


def publish_submodule(repo_root: Path, sub_path: str):
    """Push the submodule's recorded pointer to its GitHub origin if missing."""
    sub = repo_root / sub_path
    pointer = submodule_pointer(repo_root, sub_path)
    git(sub, "fetch", "origin")
    branch = origin_default_branch(sub)
    if is_ancestor(sub, pointer, f"origin/{branch}"):
        print(f"  {sub_path}: {pointer[:12]} already on origin/{branch}")
    else:
        git(sub, "push", "origin", f"{pointer}:refs/heads/{branch}")
        print(f"  {sub_path}: pushed {pointer[:12]} -> origin/{branch}")


def teardown_merged_worktrees(repo_root: Path):
    """Remove every worktree whose branch is now merged into `main`.

    Done host-side with rm + prune rather than `git worktree remove`, which
    chokes on the container-absolute gitdir pointers baked into the worktree.
    """
    for worktree in secondary_worktrees(repo_root):
        if worktree.branch is None or not is_ancestor(repo_root, worktree.branch, "main"):
            continue
        shutil.rmtree(worktree.path, ignore_errors=True)
        git(repo_root, "worktree", "prune")
        git(repo_root, "branch", "-d", worktree.branch)
        print(f"  removed merged worktree {worktree.path} ({worktree.branch})")


def publish(repo_root: Path):
    if in_docker_container():
        raise SystemExit(
            "git publish runs on the HOST, where the GitHub credentials live -- not in "
            "the container. Accept PRs in the container/browser; publish from the host."
        )
    print("Syncing local main with Gitea and GitHub...")
    sync_main(repo_root)
    entries = gitmodule_entries(repo_root)
    for _, sub_path in entries:
        sync_submodule(repo_root, sub_path)
    for name, sub_path in entries:
        offer_pointer_bump(repo_root, name, sub_path)
    print("Publishing to GitHub origin (submodules first)...")
    for _, sub_path in entries:
        publish_submodule(repo_root, sub_path)
    git(repo_root, "push", "origin", "main")
    print("  superproject pushed -> origin/main")
    print("Cleaning up merged worktrees...")
    teardown_merged_worktrees(repo_root)
    print("Published.")


def main(cfg: DevenvConfig):
    publish(cfg.repo_root)


if __name__ == "__main__":
    try:
        main(load_config(Path(__file__).resolve().parents[2]))
    except subprocess.CalledProcessError as err:
        # A failing git command has already printed its own diagnostics to
        # stderr; a Python traceback on top of them is pure noise. Exit with
        # the command's status instead.
        raise SystemExit(err.returncode) from None
