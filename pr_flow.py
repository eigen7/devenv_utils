#!/usr/bin/env python3
"""Drive the worktree-and-PR review workflow end to end.

Every change to a consumer repo lands through a pull request on the local
Gitea service, which the user reviews from the host browser (see GITEA.md).
Run this module directly from a consumer repo -- it reads that repo's
devenv.toml (see config.load_config) -- so a task is these commands:

  submodules/devenv_utils/pr_flow.py worktree <branch>
      New worktree at <worktrees_dir>/<branch> on a new branch <branch>, with
      submodules populated, the primary checkout's .env.json setup stamp copied
      over, and a Claude commit identity, so the PR distinguishes Claude's
      commits from the user's.

  submodules/devenv_utils/pr_flow.py create <branch> --title ... [--body-file ... | --body ...]
      Push the branch to the Gitea service and open its PR -- plus a PR in
      each submodule the branch advances (they merge first) -- as the
      dedicated `claude` Gitea user (see gitea_client.py) so Gitea shows
      Claude, not the reviewing admin, as pusher and author. Prints the
      review + merge handoff.

  submodules/devenv_utils/pr_flow.py abandon <branch>
      Tear down a worktree and delete its (possibly unmerged) branch, without
      touching Gitea. For worktrees whose task was dropped mid-flight -- the
      cleanup the stale-worktree report points at.

Every subcommand resolves the primary checkout itself (see
worktrees.primary_worktree) and runs its git operations there, so it behaves
identically whether invoked from the primary checkout or from inside a feature
worktree -- cfg.repo_root (the invoking checkout) is only a starting anchor,
never assumed to be the primary checkout.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (submodules/devenv_utils/pr_flow.py):
    # put the repo root on sys.path and adopt the package identity so the
    # relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import argparse
import shutil
import subprocess

from .config import DevenvConfig, load_config
from .gitea_client import (
    CLAUDE_EMAIL,
    GiteaAccess,
    container_access,
    ensure_project_remotes,
    gitea_repo_name,
    gitmodule_entries,
    register_repo,
)
from .stale_worktrees import print_stale_report
from .worktrees import primary_worktree, worktree_for_branch


def run(cmd: list[str], cwd: Path):
    subprocess.run(cmd, check=True, cwd=cwd)


def init_submodules(main: Path, worktree: Path):
    """Populate a fresh worktree's submodules from the primary checkout's copies.

    Cloning from the real upstream can fail: a submodule pointer may reference
    a commit that so far exists only in the primary checkout (submodule commits
    are pushed upstream by the user, possibly after the pointer bump lands;
    see SUBMODULES.md). The primary checkout always has the commit, so clone
    from it -- allowing direct-SHA fetch, since the pointer may be a detached
    commit there.
    """
    run(["git", "submodule", "init"], cwd=worktree)
    entries = gitmodule_entries(worktree)
    for name, sub_path in entries:
        local_copy = main / sub_path
        run(["git", "config", "uploadpack.allowAnySHA1InWant", "true"], cwd=local_copy)
        run(["git", "config", f"submodule.{name}.url", str(local_copy)], cwd=worktree)
    # protocol.file.allow: git blocks file-path submodule clones by default
    # (CVE-2022-39253 hardening); the primary checkout is trusted.
    run(["git", "-c", "protocol.file.allow=always", "submodule", "update"], cwd=worktree)
    # The same Claude identity cmd_worktree gives the superproject worktree:
    # editing a submodule in place is part of the documented workflow (see
    # SUBMODULES.md), and its commits belong to the same PR authorship.
    for _, sub_path in entries:
        run(["git", "config", "user.name", "Claude"], cwd=worktree / sub_path)
        run(["git", "config", "user.email", CLAUDE_EMAIL], cwd=worktree / sub_path)


def copy_setup_state(main: Path, cfg: DevenvConfig, worktree: Path):
    """Copy the primary checkout's .env.json into a fresh worktree.

    The file is untracked per-checkout state: the setup wizard's version stamp
    (which every entry point gates on via check_setup_version) plus any env
    mappings the wizard recorded. A worktree without it refuses to build until
    the wizard is re-run, yet the primary checkout's completed setup already
    covers it -- both checkouts live in the same container and share the same
    machine-level provisioning -- so the stamp is copied rather than
    re-earned.
    """
    rel = cfg.env_json_path.relative_to(cfg.repo_root)
    shutil.copy2(main / rel, worktree / rel)


def submodule_pointer(root: Path, sub_path: str) -> str:
    """The submodule commit recorded in root's HEAD tree."""
    entry = subprocess.run(
        ["git", "ls-tree", "HEAD", sub_path], capture_output=True, text=True, check=True, cwd=root
    ).stdout.split()
    return entry[2]


def commit_present(repo: Path, sha: str) -> bool:
    return (
        subprocess.run(["git", "cat-file", "-e", sha], cwd=repo, capture_output=True).returncode
        == 0
    )


def is_ancestor(repo: Path, maybe_ancestor: str, of: str) -> bool:
    """Whether `maybe_ancestor` is an ancestor of `of` in `repo`. A commit git
    cannot resolve (absent from the checkout) counts as not-an-ancestor."""
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", maybe_ancestor, of],
            cwd=repo,
            capture_output=True,
        ).returncode
        == 0
    )


def branch_adds_commits(main: Path, branch: str, base: str = "main") -> bool:
    """Whether `branch` carries commits `base` lacks.

    False when the branch tip equals or trails `base` -- notably a
    submodule-only change, whose commits live in the submodule repo with no
    superproject pointer bump, leaves the consumer branch even with `main`. Such
    a branch has nothing to review in the consumer repo, so no consumer PR is
    opened (`git publish` offers the pointer bump after the submodule PR merges).
    """
    return bool(git_out(main, "rev-list", f"{base}..{branch}"))


def local_branch_exists(main: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=main
    )
    return result.returncode == 0


def delete_local_branch(main: Path, branch: str, *, force: bool):
    """Delete `branch` in the primary checkout, tolerating its prior absence.

    Idempotent so a re-run reaches the same end state. `force` selects -D
    (discard even if unmerged) over -d (refuse to drop unmerged work).
    """
    flag = "-D" if force else "-d"
    result = subprocess.run(
        ["git", "branch", flag, branch], cwd=main, capture_output=True, text=True
    )
    if result.returncode and "not found" not in result.stderr:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )


def teardown_branch(main: Path, branch: str, *, force: bool) -> bool:
    """Remove `branch`'s worktree (if any), then delete the branch. Returns
    whether a worktree was actually removed.

    Order matters: git refuses to delete a branch that is checked out in a
    live worktree, so the worktree goes first. Both steps are idempotent.
    """
    worktree = worktree_for_branch(main, branch)
    if worktree is not None:
        # --force: git refuses to remove a worktree with populated submodules.
        run(["git", "worktree", "remove", "--force", str(worktree)], cwd=main)
    delete_local_branch(main, branch, force=force)
    return worktree is not None


def cmd_worktree(cfg: DevenvConfig, args: argparse.Namespace):
    main = primary_worktree(cfg.repo_root)
    print(f"Primary checkout: {main}")
    path = cfg.worktrees_dir / args.branch
    run(["git", "worktree", "add", str(path), "-b", args.branch], cwd=main)
    # Worktrees don't inherit the primary checkout's submodules or its untracked
    # setup stamp.
    init_submodules(main, path)
    copy_setup_state(main, cfg, path)
    run(["git", "config", "extensions.worktreeConfig", "true"], cwd=main)
    run(["git", "config", "--worktree", "user.name", "Claude"], cwd=path)
    run(["git", "config", "--worktree", "user.email", CLAUDE_EMAIL], cwd=path)
    print(f"Worktree ready: {path}")
    print_stale_report(cfg)


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def seed_repo_main(access: GiteaAccess, owner: str, repo: str, admin: dict, sub: Path, base: str):
    """Give a submodule's server-side repo a `main` at commit `base`, creating
    the repo itself through Gitea's push-to-create (ENABLE_PUSH_CREATE_USER).
    `base` is the published state a PR should diff against."""
    url = access.authed_repo_url(owner, repo, admin)
    run(["git", "push", url, f"{base}:refs/heads/main"], cwd=sub)


def grant_claude_write(access: GiteaAccess, owner: str, repo: str, admin: dict, claude: dict):
    """Idempotent; also repairs a claude user that lost repo access."""
    access.api(
        "PUT",
        f"/repos/{owner}/{repo}/collaborators/{claude['username']}",
        admin,
        {"permission": "write"},
    )


def open_pr(access: GiteaAccess, owner: str, repo: str, claude: dict, fields: dict):
    """Open a PR on `owner/repo`; returns (number, browser URL)."""
    pr = access.api("POST", f"/repos/{owner}/{repo}/pulls", claude, fields)
    return pr["number"], access.browser_url(f"{owner}/{repo}/pulls/{pr['number']}")


def open_submodule_prs(
    primary: Path,
    worktree: Path,
    access: GiteaAccess,
    owner: str,
    admin: dict,
    claude: dict,
    args,
) -> list[tuple[str, int, str]]:
    """Open a Gitea PR for each submodule this branch advances (they merge first).

    A submodule the branch didn't touch still points at its Gitea main, so it is
    skipped. A submodule repo the server doesn't have yet (or one with no `main`)
    is seeded first, with `main` at the pointer the primary checkout's HEAD
    records -- the published base a PR should diff against. Returns
    (repo, number, url) per opened PR.
    """
    opened = []
    for _, sub_path in gitmodule_entries(worktree):
        sub = worktree / sub_path
        head = git_out(sub, "rev-parse", "HEAD")
        repo = gitea_repo_name(sub)
        read_url = access.read_repo_url(owner, repo)
        listing = subprocess.run(
            ["git", "ls-remote", read_url, "main"],
            cwd=sub,
            capture_output=True,
            text=True,
        )
        gitea_main = listing.stdout.split()[0] if listing.returncode == 0 and listing.stdout else ""
        if gitea_main:
            # Fetch Gitea's main so its tip is a local object the ancestry check
            # below can resolve.
            subprocess.run(["git", "fetch", "--quiet", read_url, "main"], cwd=sub, check=False)
        else:
            gitea_main = submodule_pointer(primary, sub_path)
            seed_repo_main(access, owner, repo, admin, sub, gitea_main)
        # Open a PR only when the submodule head truly has new commits -- i.e. it
        # is not already contained in Gitea's main. A head that merely equals or
        # trails Gitea's main (e.g. a stale checkout) has nothing to review.
        if is_ancestor(sub, head, gitea_main):
            continue
        grant_claude_write(access, owner, repo, admin, claude)
        run(
            [
                "git",
                "push",
                access.authed_repo_url(owner, repo, claude),
                f"{head}:refs/heads/{args.branch}",
            ],
            cwd=sub,
        )
        body = Path(args.body_file).read_text() if args.body_file else args.body
        number, url = open_pr(
            access,
            owner,
            repo,
            claude,
            {"title": args.title, "head": args.branch, "base": "main", "body": body},
        )
        opened.append((repo, number, url))
    return opened


def submodule_pr_note(sub_prs: list[tuple[str, int, str]]) -> str:
    links = "\n".join(f"- {repo} #{number}: {url}" for repo, number, url in sub_prs)
    return f"\n\n---\nSubmodule PR(s), merge first:\n{links}\n"


def print_handoff(sub_prs: list[tuple[str, int, str]], repo: str, number: int, url: str):
    print("\nReview + merge on Gitea (submodule PR(s) first), then `git publish` on the host:")
    for sub_repo, sub_number, sub_url in sub_prs:
        print(f"  {sub_repo} #{sub_number}: {sub_url}")
    print(f"  {repo} #{number}: {url}")
    print("Merge each on its page, or in the container: gitea_merge.py <repo> <N>.")


def print_submodule_only_handoff(sub_prs: list[tuple[str, int, str]], repo: str):
    """Handoff for a branch that advances only submodules: no consumer PR was
    opened, so merge the submodule PR(s) and let `git publish` bump the
    pointer."""
    print(f"\nBranch adds no {repo} commits (submodule-only change), so no {repo} PR was opened.")
    print("Review + merge the submodule PR(s) on Gitea, then run `git publish` on the host --")
    print("it offers to bump the submodule pointer after the merge:")
    for sub_repo, sub_number, sub_url in sub_prs:
        print(f"  {sub_repo} #{sub_number}: {sub_url}")
    print("Merge each on its page, or in the container: gitea_merge.py <repo> <N>.")


def cmd_create(cfg: DevenvConfig, args: argparse.Namespace):
    main = primary_worktree(cfg.repo_root)
    print(f"Primary checkout: {main}")
    access = container_access()
    access.ensure_reachable()
    admin = access.admin_creds()
    claude = access.claude_creds()
    owner = admin["username"]
    ensure_project_remotes(access, main, cfg.name, owner)
    register_repo(access, main, cfg.name, owner)

    # A coordinated change has a PR in each touched submodule too; open those
    # first (they merge first) and cross-reference them from the consumer PR.
    worktree = worktree_for_branch(main, args.branch)
    sub_prs = (
        open_submodule_prs(main, worktree, access, owner, admin, claude, args)
        if worktree is not None
        else []
    )

    # A branch that adds no consumer commits (a submodule-only change) has
    # nothing to review in the consumer repo: skip the empty PR. The submodule
    # PR(s) still open above, and `git publish` offers the pointer bump after
    # they merge.
    if not branch_adds_commits(main, args.branch):
        if sub_prs:
            print_submodule_only_handoff(sub_prs, cfg.name)
        else:
            print(
                f"Branch {args.branch} adds no commits and touches no submodule; nothing to open."
            )
        print_stale_report(cfg)
        return

    grant_claude_write(access, owner, cfg.name, admin, claude)
    # Pushing from the primary checkout satisfies push.recurseSubmodules=check:
    # its submodule clones carry the gitea remote-tracking refs a worktree's
    # freshly file-cloned submodules lack.
    run(
        ["git", "push", access.authed_repo_url(owner, cfg.name, claude), args.branch],
        cwd=main,
    )

    body = Path(args.body_file).read_text() if args.body_file else args.body
    if sub_prs:
        body += submodule_pr_note(sub_prs)
    number, url = open_pr(
        access,
        owner,
        cfg.name,
        claude,
        {"title": args.title, "head": args.branch, "base": "main", "body": body},
    )
    print_handoff(sub_prs, cfg.name, number, url)
    print_stale_report(cfg)


def cmd_abandon(cfg: DevenvConfig, args: argparse.Namespace):
    main = primary_worktree(cfg.repo_root)
    print(f"Primary checkout: {main}")
    branch = args.branch
    if worktree_for_branch(main, branch) is None and not local_branch_exists(main, branch):
        print(f"No worktree or branch named {branch}; nothing to abandon.")
        return
    # force=True: an abandoned branch is typically unmerged, and the user has
    # deliberately chosen to discard it (see the stale-worktree report).
    removed = teardown_branch(main, branch, force=True)
    what = "worktree removed and branch deleted" if removed else "branch deleted (no worktree)"
    print(f"Abandoned {branch}: {what}.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("worktree", help="create a worktree + branch with a Claude identity")
    p.add_argument("branch", help="branch (and worktree directory) name")
    p.set_defaults(func=cmd_worktree)

    p = sub.add_parser("create", help="push a branch and open its PR(s) as the claude user")
    p.add_argument("branch", help="branch to push and open a PR for")
    p.add_argument("--title", required=True, help="PR title")
    body = p.add_mutually_exclusive_group()
    body.add_argument("--body-file", help="file holding the PR description (markdown)")
    body.add_argument("--body", default="", help="inline PR description")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser(
        "abandon", help="tear down a worktree and delete its branch (local only, no Gitea)"
    )
    p.add_argument("branch", help="branch whose worktree + local branch to remove")
    p.set_defaults(func=cmd_abandon)

    return parser.parse_args()


def main(cfg: DevenvConfig):
    args = parse_args()
    args.func(cfg, args)


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
