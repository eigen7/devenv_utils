"""Drive the worktree-and-PR review workflow end to end.

Every change to a consumer repo lands through a pull request on the local
Gitea instance, which the user reviews from the host browser (see
gitea_serve.py). Consumer repos expose this module through a thin
py/tools/pr.py shim that passes their DevenvConfig, so a task is these
commands:

  py/tools/pr.py worktree <branch>
      New worktree at <worktrees_dir>/<branch> on a new branch <branch>, with
      submodules populated, the primary checkout's .env.json setup stamp copied
      over, and a Claude commit identity, so the PR distinguishes Claude's
      commits from the user's.

  py/tools/pr.py create <branch> --title ... [--body-file ... | --body ...]
      Start the Gitea stack if needed, push the branch, and open the PR --
      both as the dedicated `claude` Gitea user (provisioned on first use,
      credentials in <mount>/gitea/claude_credentials.json) so Gitea shows
      Claude, not the reviewing admin, as pusher and author. Prints the
      review URL.

  py/tools/pr.py merge <N>
      After the user approves: merge the PR (as the admin), fast-forward the
      primary checkout, and delete the branch (local and remote) and its
      worktree. Idempotent: safe to re-run after a partial failure.

  py/tools/pr.py abandon <branch>
      Tear down a worktree and delete its (possibly unmerged) branch, without
      touching Gitea. For worktrees whose task was dropped mid-flight -- the
      cleanup the stale-worktree report points at.

Every subcommand resolves the primary checkout itself (see
worktrees.primary_worktree) and runs its git operations there, so it behaves
identically whether invoked from the primary checkout or from inside a feature
worktree -- the shim's cfg.repo_root is only a starting anchor, never assumed
to be the primary checkout.
"""

import argparse
import base64
import json
import secrets
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .config import DevenvConfig
from .gitea_serve import GITEA_ROOT, ensure_serving
from .worktrees import primary_worktree, worktree_for_branch

CLAUDE_CREDENTIALS_PATH = GITEA_ROOT / "claude_credentials.json"
CLAUDE_USER = "claude"
# Matches the commit identity, so Gitea links Claude's commits to the user.
CLAUDE_EMAIL = "noreply@anthropic.com"


def run(cmd: list[str], cwd: Path):
    subprocess.run(cmd, check=True, cwd=cwd)


def api(method: str, backend_port: int, path: str, creds: dict, payload: dict | None = None):
    """A Gitea API call against the loopback backend port (plain basic auth,
    no reverse-proxy header stamping). Returns the decoded JSON response, or
    None for empty bodies."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{backend_port}/api/v1{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
    )
    auth = base64.b64encode(f"{creds['username']}:{creds['password']}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
    return json.loads(body) if body else None


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


def delete_remote_branch(backend_port: int, owner: str, name: str, branch: str, admin: dict):
    """Delete the PR's remote branch, tolerating its prior deletion so a
    re-run after a partial failure succeeds."""
    try:
        api("DELETE", backend_port, f"/repos/{owner}/{name}/branches/{branch}", admin)
    except urllib.error.HTTPError as err:
        if err.code != 404:
            raise


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


def has_remote(repo: Path, remote: str) -> bool:
    return (
        subprocess.run(
            ["git", "remote", "get-url", remote], cwd=repo, capture_output=True
        ).returncode
        == 0
    )


def sync_submodules(main: Path):
    """Check out each submodule to the pointer recorded in main's HEAD, fetching
    any missing commit from gitea rather than the submodule's upstream origin.

    At merge time the newly referenced submodule commit is on gitea -- that is
    where it was reviewed -- but has not necessarily reached the submodule's
    upstream origin yet; that push is a separate, later host-side step (see
    SUBMODULES.md). Letting a `submodule.recurse` pull update the submodule
    would fetch it from origin and fail on such a commit, so cmd_merge pulls
    without recursing and calls this instead: fetch from gitea when the commit
    is absent, then check out from the now-local objects.
    """
    for _, sub_path in gitmodule_entries(main):
        sub = main / sub_path
        if not commit_present(sub, submodule_pointer(main, sub_path)) and has_remote(sub, "gitea"):
            run(["git", "fetch", "-q", "gitea"], cwd=sub)
    # protocol.file.allow: inert for the http(s) submodule remotes of a real
    # primary checkout, but lets file-path remotes (tests) work through the
    # submodule-context command.
    run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"],
        cwd=main,
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


def teardown_branch(main: Path, branch: str, *, force: bool):
    """Remove `branch`'s worktree (if any), then delete the branch.

    Order matters: git refuses to delete a branch that is checked out in a
    live worktree, so the worktree goes first. Both steps are idempotent.
    """
    worktree = worktree_for_branch(main, branch)
    if worktree is not None:
        # --force: git refuses to remove a worktree with populated submodules.
        run(["git", "worktree", "remove", "--force", str(worktree)], cwd=main)
    delete_local_branch(main, branch, force=force)


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


def cmd_create(cfg: DevenvConfig, args: argparse.Namespace):
    main = primary_worktree(cfg.repo_root)
    print(f"Primary checkout: {main}")
    admin, web_port, backend_port = ensure_serving(cfg)
    owner = admin["username"]
    claude = ensure_claude_user(admin, backend_port)
    # Idempotent; also repairs a claude user that lost repo access.
    api(
        "PUT",
        backend_port,
        f"/repos/{owner}/{cfg.name}/collaborators/{CLAUDE_USER}",
        admin,
        {"permission": "write"},
    )

    # The `gitea` remote embeds the admin's credentials, so the push goes
    # through an explicit claude-credentialed URL instead. Pushing from the
    # primary checkout also satisfies push.recurseSubmodules=check: its
    # submodule clones carry the gitea remote-tracking refs a worktree's
    # freshly file-cloned submodules lack.
    push_url = (
        f"http://{CLAUDE_USER}:{claude['password']}@localhost:{backend_port}/{owner}/{cfg.name}.git"
    )
    run(["git", "push", push_url, args.branch], cwd=main)

    body = Path(args.body_file).read_text() if args.body_file else args.body
    pr = api(
        "POST",
        backend_port,
        f"/repos/{owner}/{cfg.name}/pulls",
        claude,
        {"title": args.title, "head": args.branch, "base": "main", "body": body},
    )
    print(
        f"PR #{pr['number']}: http://localhost:{web_port}/{owner}/{cfg.name}/pulls/{pr['number']}"
    )


def cmd_merge(cfg: DevenvConfig, args: argparse.Namespace):
    main = primary_worktree(cfg.repo_root)
    print(f"Primary checkout: {main}")
    admin, _, backend_port = ensure_serving(cfg)
    owner = admin["username"]
    pr = api("GET", backend_port, f"/repos/{owner}/{cfg.name}/pulls/{args.number}", admin)
    branch = pr["head"]["ref"]
    # Each step below is idempotent, so a re-run after a partial failure
    # completes the cleanup rather than erroring.
    if not pr["merged"]:
        api(
            "POST",
            backend_port,
            f"/repos/{owner}/{cfg.name}/pulls/{args.number}/merge",
            admin,
            {"Do": "merge"},
        )
    # Pull without recursing into submodules; sync_submodules then updates them
    # from gitea, which serves any freshly referenced submodule commit even
    # before it reaches the submodule's upstream origin.
    run(["git", "-c", "submodule.recurse=false", "pull", "--ff-only", "gitea", "main"], cwd=main)
    sync_submodules(main)
    delete_remote_branch(backend_port, owner, cfg.name, branch, admin)
    teardown_branch(main, branch, force=False)
    print(
        f"PR #{args.number} ({branch}) merged; primary checkout fast-forwarded, worktree removed."
    )


def cmd_abandon(cfg: DevenvConfig, args: argparse.Namespace):
    main = primary_worktree(cfg.repo_root)
    print(f"Primary checkout: {main}")
    branch = args.branch
    if worktree_for_branch(main, branch) is None and not local_branch_exists(main, branch):
        print(f"No worktree or branch named {branch}; nothing to abandon.")
        return
    # force=True: an abandoned branch is typically unmerged, and the user has
    # deliberately chosen to discard it (see the stale-worktree report).
    teardown_branch(main, branch, force=True)
    print(f"Abandoned {branch}: worktree removed and branch deleted.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("worktree", help="create a worktree + branch with a Claude identity")
    p.add_argument("branch", help="branch (and worktree directory) name")
    p.set_defaults(func=cmd_worktree)

    p = sub.add_parser("create", help="push a branch and open its PR as the claude user")
    p.add_argument("branch", help="branch to push and open a PR for")
    p.add_argument("--title", required=True, help="PR title")
    body = p.add_mutually_exclusive_group()
    body.add_argument("--body-file", help="file holding the PR description (markdown)")
    body.add_argument("--body", default="", help="inline PR description")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser(
        "merge", help="merge an approved PR, fast-forward main, clean up branch + worktree"
    )
    p.add_argument("number", type=int, help="PR number")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser(
        "abandon", help="tear down a worktree and delete its branch (local only, no Gitea)"
    )
    p.add_argument("branch", help="branch whose worktree + local branch to remove")
    p.set_defaults(func=cmd_abandon)

    return parser.parse_args()


def main(cfg: DevenvConfig):
    args = parse_args()
    args.func(cfg, args)
