"""Drive the worktree-and-PR review workflow end to end.

Every change to a consumer repo lands through a pull request on the local
Gitea instance, which the user reviews from the host browser (see
gitea_serve.py). Consumer repos expose this module through a thin
py/tools/pr.py shim that passes their DevenvConfig, so a task is three
commands:

  py/tools/pr.py worktree <branch>
      New worktree at <worktrees_dir>/<branch> on a new branch <branch>, with
      submodules populated, the main checkout's .env.json setup stamp copied
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
      main checkout, and delete the branch (local and remote) and its
      worktree.
"""

import argparse
import base64
import json
import secrets
import shutil
import subprocess
import urllib.request
from pathlib import Path

from .config import DevenvConfig
from .gitea_serve import GITEA_ROOT, ensure_serving

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


def init_submodules(cfg: DevenvConfig, worktree: Path):
    """Populate a fresh worktree's submodules from the main checkout's copies.

    Cloning from the real upstream can fail: a submodule pointer may reference
    a commit that so far exists only in the main checkout (submodule commits
    are pushed upstream by the user, possibly after the pointer bump lands;
    see SUBMODULES.md). The main checkout always has the commit, so clone from
    it -- allowing direct-SHA fetch, since the pointer may be a detached
    commit there.
    """
    run(["git", "submodule", "init"], cwd=worktree)
    listing = subprocess.run(
        ["git", "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        capture_output=True,
        text=True,
        check=True,
        cwd=worktree,
    )
    sub_paths = []
    for line in listing.stdout.splitlines():
        key, sub_path = line.split(" ", 1)
        name = key.removeprefix("submodule.").removesuffix(".path")
        sub_paths.append(sub_path)
        local_copy = cfg.repo_root / sub_path
        run(["git", "config", "uploadpack.allowAnySHA1InWant", "true"], cwd=local_copy)
        run(["git", "config", f"submodule.{name}.url", str(local_copy)], cwd=worktree)
    # protocol.file.allow: git blocks file-path submodule clones by default
    # (CVE-2022-39253 hardening); the main checkout is trusted.
    run(["git", "-c", "protocol.file.allow=always", "submodule", "update"], cwd=worktree)
    # The same Claude identity cmd_worktree gives the superproject worktree:
    # editing a submodule in place is part of the documented workflow (see
    # SUBMODULES.md), and its commits belong to the same PR authorship.
    for sub_path in sub_paths:
        run(["git", "config", "user.name", "Claude"], cwd=worktree / sub_path)
        run(["git", "config", "user.email", CLAUDE_EMAIL], cwd=worktree / sub_path)


def copy_setup_state(cfg: DevenvConfig, worktree: Path):
    """Copy the main checkout's .env.json into a fresh worktree.

    The file is untracked per-checkout state: the setup wizard's version stamp
    (which every entry point gates on via check_setup_version) plus any env
    mappings the wizard recorded. A worktree without it refuses to build until
    the wizard is re-run, yet the main checkout's completed setup already
    covers it -- both checkouts live in the same container and share the same
    machine-level provisioning -- so the stamp is copied rather than
    re-earned.
    """
    rel = cfg.env_json_path.relative_to(cfg.repo_root)
    shutil.copy2(cfg.env_json_path, worktree / rel)


def worktree_path_for(cfg: DevenvConfig, branch: str) -> Path | None:
    """The path of the worktree that has `branch` checked out, if any."""
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
        cwd=cfg.repo_root,
    )
    path = None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}" and path != Path(cfg.repo_root):
            return path
    return None


def cmd_worktree(cfg: DevenvConfig, args: argparse.Namespace):
    path = cfg.worktrees_dir / args.branch
    run(["git", "worktree", "add", str(path), "-b", args.branch], cwd=cfg.repo_root)
    # Worktrees don't inherit the main checkout's submodules or its untracked
    # setup stamp.
    init_submodules(cfg, path)
    copy_setup_state(cfg, path)
    run(["git", "config", "extensions.worktreeConfig", "true"], cwd=cfg.repo_root)
    run(["git", "config", "--worktree", "user.name", "Claude"], cwd=path)
    run(["git", "config", "--worktree", "user.email", CLAUDE_EMAIL], cwd=path)
    print(f"Worktree ready: {path}")


def cmd_create(cfg: DevenvConfig, args: argparse.Namespace):
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
    # through an explicit claude-credentialed URL instead.
    push_url = (
        f"http://{CLAUDE_USER}:{claude['password']}@localhost:{backend_port}/{owner}/{cfg.name}.git"
    )
    run(["git", "push", push_url, args.branch], cwd=cfg.repo_root)

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
    admin, _, backend_port = ensure_serving(cfg)
    owner = admin["username"]
    pr = api("GET", backend_port, f"/repos/{owner}/{cfg.name}/pulls/{args.number}", admin)
    branch = pr["head"]["ref"]
    # Skipped when re-run after a partial failure below.
    if not pr["merged"]:
        api(
            "POST",
            backend_port,
            f"/repos/{owner}/{cfg.name}/pulls/{args.number}/merge",
            admin,
            {"Do": "merge"},
        )
    run(["git", "pull", "--ff-only", "gitea", "main"], cwd=cfg.repo_root)
    api("DELETE", backend_port, f"/repos/{owner}/{cfg.name}/branches/{branch}", admin)
    worktree = worktree_path_for(cfg, branch)
    if worktree is not None:
        # --force: git refuses to remove a worktree whose submodule is populated.
        run(["git", "worktree", "remove", "--force", str(worktree)], cwd=cfg.repo_root)
    run(["git", "branch", "-d", branch], cwd=cfg.repo_root)
    print(f"PR #{args.number} ({branch}) merged; main checkout fast-forwarded, worktree removed.")


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

    return parser.parse_args()


def main(cfg: DevenvConfig):
    args = parse_args()
    args.func(cfg, args)
