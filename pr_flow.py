#!/usr/bin/env python3
"""Drive the worktree-and-PR review workflow end to end.

Every change to a consumer repo lands through a pull request on the local
Gitea instance, which the user reviews from the host browser (see
gitea_serve.py). Run this module directly from a consumer repo -- it reads that
repo's devenv.toml (see config.load_config) -- so a task is these commands:

  submodules/devenv_utils/pr_flow.py worktree <branch>
      New worktree at <worktrees_dir>/<branch> on a new branch <branch>, with
      submodules populated, the primary checkout's .env.json setup stamp copied
      over, and a Claude commit identity, so the PR distinguishes Claude's
      commits from the user's.

  submodules/devenv_utils/pr_flow.py create <branch> --title ... [--body-file ... | --body ...]
      Start the Gitea stack if needed, push the branch, and open its PR -- plus
      a PR in each submodule the branch advances (they merge first) -- as the
      dedicated `claude` Gitea user (provisioned on first use, credentials in
      <mount>/gitea/claude_credentials.json) so Gitea shows Claude, not the
      reviewing admin, as pusher and author. Prints the review + merge handoff.

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
import json
import secrets
import shutil
import subprocess
import urllib.parse

from .config import DevenvConfig, load_config
from .gitea_serve import GITEA_ROOT, api, ensure_serving
from .worktrees import primary_worktree, worktree_for_branch

CLAUDE_CREDENTIALS_PATH = GITEA_ROOT / "claude_credentials.json"
CLAUDE_USER = "claude"
# Matches the commit identity, so Gitea links Claude's commits to the user.
CLAUDE_EMAIL = "noreply@anthropic.com"


def run(cmd: list[str], cwd: Path):
    subprocess.run(cmd, check=True, cwd=cwd)


def ensure_claude_user(admin: dict, backend_port: int) -> dict:
    """The `claude` Gitea user's credentials, provisioning the user (and the
    600-mode credentials file) on first use."""
    if CLAUDE_CREDENTIALS_PATH.exists():
        return json.loads(CLAUDE_CREDENTIALS_PATH.read_text())
    creds = {"username": CLAUDE_USER, "password": secrets.token_urlsafe(16)}
    api(
        "POST",
        backend_port,
        "/admin/users",
        admin,
        {
            "username": creds["username"],
            "email": CLAUDE_EMAIL,
            "password": creds["password"],
            "must_change_password": False,
        },
    )
    CLAUDE_CREDENTIALS_PATH.write_text(json.dumps(creds, indent=2) + "\n")
    CLAUDE_CREDENTIALS_PATH.chmod(0o600)
    return creds


def gitmodule_entries(root: Path) -> list[tuple[str, str]]:
    """(name, path) for each submodule declared in root's .gitmodules."""
    listing = subprocess.run(
        ["git", "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    )
    entries = []
    for line in listing.stdout.splitlines():
        key, path = line.split(" ", 1)
        entries.append((key.removeprefix("submodule.").removesuffix(".path"), path))
    return entries


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


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def push_url(backend_port: int, owner: str, repo: str, user: str, password: str) -> str:
    """An authenticated backend-port push URL for a server-side repo."""
    return f"http://{user}:{password}@localhost:{backend_port}/{owner}/{repo}.git"


def claude_push_url(backend_port: int, owner: str, repo: str, claude: dict) -> str:
    """A claude-credentialed push URL (the `gitea` remote embeds the admin's)."""
    return push_url(backend_port, owner, repo, CLAUDE_USER, claude["password"])


def seed_repo_main(backend_port: int, owner: str, repo: str, admin: dict, sub: Path, base: str):
    """Give a submodule's server-side repo a `main` at commit `base`, creating
    the repo itself through Gitea's push-to-create (ENABLE_PUSH_CREATE_USER).
    `base` is the published state a PR should diff against."""
    url = push_url(backend_port, owner, repo, admin["username"], admin["password"])
    run(["git", "push", url, f"{base}:refs/heads/main"], cwd=sub)


def grant_claude_write(backend_port: int, owner: str, repo: str, admin: dict):
    """Idempotent; also repairs a claude user that lost repo access."""
    api(
        "PUT",
        backend_port,
        f"/repos/{owner}/{repo}/collaborators/{CLAUDE_USER}",
        admin,
        {"permission": "write"},
    )


def open_pr(backend_port: int, web_port: int, owner: str, repo: str, claude: dict, fields: dict):
    """Open a PR on `owner/repo`; returns (number, web URL)."""
    pr = api("POST", backend_port, f"/repos/{owner}/{repo}/pulls", claude, fields)
    return pr["number"], f"http://localhost:{web_port}/{owner}/{repo}/pulls/{pr['number']}"


def gitea_repo_name(sub_dir: Path) -> str:
    """A submodule's Gitea repo name -- its origin basename (same project)."""
    return Path(urllib.parse.urlparse(git_out(sub_dir, "remote", "get-url", "origin")).path).stem


def open_submodule_prs(
    primary: Path,
    worktree: Path,
    backend_port: int,
    web_port: int,
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
        listing = subprocess.run(
            ["git", "ls-remote", f"http://localhost:{backend_port}/{owner}/{repo}.git", "main"],
            cwd=sub,
            capture_output=True,
            text=True,
        )
        gitea_main = listing.stdout.split()[0] if listing.returncode == 0 and listing.stdout else ""
        if not gitea_main:
            gitea_main = submodule_pointer(primary, sub_path)
            seed_repo_main(backend_port, owner, repo, admin, sub, gitea_main)
        if head == gitea_main:
            continue
        grant_claude_write(backend_port, owner, repo, admin)
        run(
            [
                "git",
                "push",
                claude_push_url(backend_port, owner, repo, claude),
                f"{head}:refs/heads/{args.branch}",
            ],
            cwd=sub,
        )
        body = Path(args.body_file).read_text() if args.body_file else args.body
        number, url = open_pr(
            backend_port,
            web_port,
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


def cmd_create(cfg: DevenvConfig, args: argparse.Namespace):
    main = primary_worktree(cfg.repo_root)
    print(f"Primary checkout: {main}")
    admin, web_port, backend_port = ensure_serving(cfg)
    owner = admin["username"]
    claude = ensure_claude_user(admin, backend_port)

    # A coordinated change has a PR in each touched submodule too; open those
    # first (they merge first) and cross-reference them from the consumer PR.
    worktree = worktree_for_branch(main, args.branch)
    sub_prs = (
        open_submodule_prs(main, worktree, backend_port, web_port, owner, admin, claude, args)
        if worktree is not None
        else []
    )

    grant_claude_write(backend_port, owner, cfg.name, admin)
    # Pushing from the primary checkout satisfies push.recurseSubmodules=check:
    # its submodule clones carry the gitea remote-tracking refs a worktree's
    # freshly file-cloned submodules lack.
    run(
        ["git", "push", claude_push_url(backend_port, owner, cfg.name, claude), args.branch],
        cwd=main,
    )

    body = Path(args.body_file).read_text() if args.body_file else args.body
    if sub_prs:
        body += submodule_pr_note(sub_prs)
    number, url = open_pr(
        backend_port,
        web_port,
        owner,
        cfg.name,
        claude,
        {"title": args.title, "head": args.branch, "base": "main", "body": body},
    )
    print_handoff(sub_prs, cfg.name, number, url)


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
