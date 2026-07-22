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
import urllib.parse

from .config import DevenvConfig, load_config
from .gitea_client import REMOTE_NAME, gitmodule_entries
from .pr_flow import commit_present, submodule_pointer
from .state import in_docker_container
from .worktrees import secondary_worktrees


def git(cwd: Path, *args: str):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def gitea_read_url(repo_root: Path, sub_path: str = "") -> str:
    """The URL of a Gitea repo as fetchable from the host, for read-only use.

    Derived from the parent's `gitea` remote -- always present, as the review
    remote. That remote holds the canonical credential-free web-port URL
    (see gitea_client.py), which resolves on the host as-is. Read from raw
    config rather than `git remote get-url`, which would bake in the caller's
    insteadOf rewrites (in a dev container, the canonical URL rewrites to the
    service-container form). A submodule's Gitea repo lives under the same
    owner, named after its GitHub origin (the same project), so it needs no
    `gitea` remote of its own -- which a fresh submodule clone lacks.
    """
    parent = urllib.parse.urlparse(git_out(repo_root, "config", f"remote.{REMOTE_NAME}.url"))
    if not sub_path:
        return parent.geturl()
    base = f"{parent.scheme}://{parent.netloc}"
    owner = parent.path.strip("/").split("/")[0]
    origin = urllib.parse.urlparse(git_out(repo_root / sub_path, "remote", "get-url", "origin"))
    name = Path(origin.path).stem
    return f"{base}/{owner}/{name}.git"


def is_ancestor(repo: Path, maybe_ancestor: str, of: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", maybe_ancestor, of], cwd=repo
        ).returncode
        == 0
    )


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


def reconcile_diverged_gitea(repo_root: Path, gitea_tip: str, origin_tip: str):
    """Reconcile a local `main` that has diverged from Gitea's.

    The recipe follows one rule: never rewrite a commit another repository
    already has. When some local-only commit is already on GitHub origin, a
    rebase would mint new hashes for published history and the final
    fast-forward push to origin would be rejected -- so merge. When every
    local-only commit is still private, rebasing onto Gitea's main rewrites
    nothing anyone else holds and keeps `main` linear."""
    local_only = commits_beyond(repo_root, "main", gitea_tip)
    private = set(commits_beyond(repo_root, "main", gitea_tip, origin_tip))
    published = [line for line in local_only if line not in private]
    if published:
        print_commits("The following commits are on GitHub but are missing from Gitea:", published)
        explanation = MERGE_GITEA_EXPLANATION.format(tip=gitea_tip[:12])
        if not confirm("Merge Gitea's main into yours?", explanation):
            raise SystemExit(DECLINED_NOTICE)
        merge_or_abort(repo_root, gitea_tip, "gitea main")
    else:
        print_commits("Gitea is missing the following local-only commits:", local_only)
        explanation = REBASE_EXPLANATION.format(tip=gitea_tip[:12])
        if not confirm("Rebase them onto Gitea's main?", explanation):
            raise SystemExit(DECLINED_NOTICE)
        rebase_or_abort(repo_root, gitea_tip)


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
    GitHub pushes that follow are guaranteed fast-forwards."""
    if git_out(repo_root, "branch", "--show-current") != "main":
        raise SystemExit("git publish must run on `main`; check it out first.")
    git(repo_root, "fetch", gitea_read_url(repo_root), "main")
    gitea_tip = git_out(repo_root, "rev-parse", "FETCH_HEAD")
    git(repo_root, "fetch", "origin", "main")
    origin_tip = git_out(repo_root, "rev-parse", "FETCH_HEAD")
    relation = main_relationship(repo_root, gitea_tip)
    if relation == "behind":
        git(repo_root, "merge", "--ff-only", gitea_tip)
    elif relation == "diverged":
        reconcile_diverged_gitea(repo_root, gitea_tip, origin_tip)
    merge_github_only_commits(repo_root, origin_tip)
    if git_out(repo_root, "rev-parse", "main") != gitea_tip:
        print("Syncing Gitea's main to the local main...")
        git(repo_root, "push", REMOTE_NAME, "main")


def sync_submodule(repo_root: Path, sub_path: str):
    """Check out the submodule to its recorded pointer, fetching from Gitea when
    the pointer commit isn't local yet (its GitHub push happens next)."""
    sub = repo_root / sub_path
    pointer = submodule_pointer(repo_root, sub_path)
    if not commit_present(sub, pointer):
        git(sub, "fetch", gitea_read_url(repo_root, sub_path))
    git(repo_root, "submodule", "update", "--init", sub_path)


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
    main(load_config(Path(__file__).resolve().parents[2]))
