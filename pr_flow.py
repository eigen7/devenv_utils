#!/usr/bin/env python3
"""Drive the worktree-and-PR workflow end to end.

Every change lands through a pull request on GitHub, which the user reviews
and merges there. Run this module directly from the repo it serves -- it reads
that repo's devenv.toml (see config.load_config) -- so a task is these
commands:

  pr_flow.py worktree <branch>
      New worktree at <worktrees_dir>/<branch> on a new branch <branch>, with
      the primary checkout's .env.json setup stamp copied over and a Claude
      commit identity, so the PR distinguishes Claude's commits from the
      user's.

  pr_flow.py create <branch> --title ... [--body-file ... | --body ...]
      Push the branch to origin and open its GitHub PR (or report the PR that
      already exists for it -- follow-up pushes update an open PR by
      themselves). Prints the review + merge handoff.

  pr_flow.py cleanup
      Remove the worktrees and local branches of every branch whose PR has
      merged. The post-merge counterpart to `worktree`.

  pr_flow.py abandon <branch>
      Tear down a worktree and delete its (possibly unmerged) branch, without
      touching GitHub. For worktrees whose task was dropped mid-flight -- the
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
    # Enable running this file directly (as a CLI tool or git hook): load the
    # package under its canonical name from this file's own directory, whatever
    # that directory is called -- subtrees/devenv_utils/ in a consumer repo,
    # the repo root (or a worktree of it, named after its branch) in
    # devenv_utils' own working clone.
    import importlib.util

    _pkg_dir = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location(
        "devenv_utils",
        _pkg_dir / "__init__.py",
        submodule_search_locations=[str(_pkg_dir)],
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["devenv_utils"] = _pkg
    _spec.loader.exec_module(_pkg)
    __package__ = "devenv_utils"

import argparse
import shutil
import subprocess

from .config import DevenvConfig, load_config
from .console import SetupException
from .github_access import api, find_open_pr, open_pr, origin_repo
from .stale_worktrees import changed_files, print_stale_report
from .worktrees import primary_worktree, secondary_worktrees, worktree_for_branch

CLAUDE_NAME = "Claude"
CLAUDE_EMAIL = "noreply@anthropic.com"


def run(cmd: list[str], cwd: Path):
    subprocess.run(cmd, check=True, cwd=cwd)


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def copy_setup_state(main: Path, cfg: DevenvConfig, worktree: Path):
    """Copy the primary checkout's .env.json into a fresh worktree.

    The file is untracked per-checkout state: the setup wizard's version stamp
    (which every entry point gates on via check_setup_version) plus any env
    mappings the wizard recorded. A worktree without it refuses to build until
    the wizard is re-run, yet the primary checkout's completed setup already
    covers it -- both checkouts live in the same container and share the same
    machine-level provisioning -- so the stamp is copied rather than
    re-earned. A repo with no wizard (devenv_utils' own clone) has no stamp to
    copy.
    """
    rel = cfg.env_json_path.relative_to(cfg.repo_root)
    if (main / rel).exists():
        shutil.copy2(main / rel, worktree / rel)


def branch_adds_commits(main: Path, branch: str, base: str = "main") -> bool:
    """Whether `branch` carries commits `base` lacks."""
    return bool(git_out(main, "rev-list", f"{base}..{branch}"))


def local_branch_exists(main: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=main, check=False
    )
    return result.returncode == 0


def delete_local_branch(main: Path, branch: str, *, force: bool):
    """Delete `branch` in the primary checkout, tolerating its prior absence.

    Idempotent so a re-run reaches the same end state. `force` selects -D
    (discard even if unmerged) over -d (refuse to drop unmerged work).
    """
    flag = "-D" if force else "-d"
    result = subprocess.run(
        ["git", "branch", flag, branch], cwd=main, capture_output=True, text=True, check=False
    )
    if result.returncode and "not found" not in result.stderr:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )


def teardown_branch(main: Path, branch: str, *, force: bool) -> bool:
    """Remove `branch`'s worktree (if any), then delete the branch. Returns
    whether a worktree was actually removed.

    Order matters: git refuses to delete a branch that is checked out in a
    live worktree, so the worktree goes first. --force, because git also
    refuses to remove a worktree holding untracked files -- build outputs make
    that the normal state here. Both steps are idempotent.
    """
    worktree = worktree_for_branch(main, branch)
    if worktree is not None:
        run(["git", "worktree", "remove", "--force", str(worktree)], cwd=main)
    delete_local_branch(main, branch, force=force)
    return worktree is not None


def cmd_worktree(cfg: DevenvConfig, args: argparse.Namespace):
    main = primary_worktree(cfg.repo_root)
    print(f"Primary checkout: {main}")
    path = cfg.worktrees_dir / args.branch
    run(["git", "worktree", "add", str(path), "-b", args.branch], cwd=main)
    # Worktrees don't inherit the primary checkout's untracked setup stamp.
    copy_setup_state(main, cfg, path)
    run(["git", "config", "extensions.worktreeConfig", "true"], cwd=main)
    run(["git", "config", "--worktree", "user.name", CLAUDE_NAME], cwd=path)
    run(["git", "config", "--worktree", "user.email", CLAUDE_EMAIL], cwd=path)
    print(f"Worktree ready: {path}")
    print_stale_report(cfg)


def print_handoff(url: str):
    print("\nReview + merge on GitHub:")
    print(f"  {url}")
    print("After the merge, `git pull` on main and run `pr_flow.py cleanup` to")
    print("remove the merged worktree.")


def cmd_create(cfg: DevenvConfig, args: argparse.Namespace):
    main = primary_worktree(cfg.repo_root)
    print(f"Primary checkout: {main}")
    if not branch_adds_commits(main, args.branch):
        print(f"Branch {args.branch} adds no commits over main; nothing to open.")
        print_stale_report(cfg)
        return
    slug = origin_repo(main)
    run(["git", "push", "-u", "origin", args.branch], cwd=main)
    existing = find_open_pr(slug, args.branch)
    if existing is not None:
        print(f"PR already open for {args.branch}; the push updated it.")
        print_handoff(existing["html_url"])
    else:
        body = Path(args.body_file).read_text() if args.body_file else args.body
        pr = open_pr(slug, head=args.branch, base="main", title=args.title, body=body)
        print_handoff(pr["html_url"])
    print_stale_report(cfg)


def branch_pr_merged(slug: str, branch: str) -> bool:
    """Whether `branch`'s PR has merged into origin's main, per GitHub's
    merged flag (which also recognizes squash and rebase merges).

    Ancestry is deliberately not consulted: reachability from origin/main is
    equivalent to "the branch has no commits of its own", which is just as
    true of a freshly prepared worktree whose work is still uncommitted --
    the case that must never read as merged. Work landed without a PR (e.g.
    pushed to main from the host) therefore leaves its worktree behind;
    `abandon` covers that, and a lingering worktree beats a destroyed one."""
    owner = slug.split("/")[0]
    prs = api("GET", f"/repos/{slug}/pulls?state=closed&head={owner}:{branch}")
    return any(pr.get("merged_at") for pr in prs)


def cmd_cleanup(cfg: DevenvConfig, args: argparse.Namespace):
    main = primary_worktree(cfg.repo_root)
    print(f"Primary checkout: {main}")
    run(["git", "fetch", "--quiet", "origin", "main"], cwd=main)
    slug = origin_repo(main)
    removed = 0
    for entry in secondary_worktrees(main):
        if entry.branch is None or not local_branch_exists(main, entry.branch):
            continue
        if not branch_pr_merged(slug, entry.branch):
            continue
        if changed_files(entry.path):
            # Removal would discard the uncommitted changes; leave the
            # worktree for the user (`abandon` once they've salvaged them).
            print(f"Skipping {entry.branch}: PR merged, but the worktree has uncommitted changes.")
            continue
        teardown_branch(main, entry.branch, force=True)
        print(f"Removed merged worktree + branch: {entry.branch}")
        removed += 1
    if not removed:
        print("No merged worktrees to clean up.")


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
    print(f"Abandoned {branch}.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("worktree", help="create a worktree + branch with a Claude identity")
    p.add_argument("branch", help="branch (and worktree directory) name")
    p.set_defaults(func=cmd_worktree)

    p = sub.add_parser("create", help="push a branch to origin and open its GitHub PR")
    p.add_argument("branch", help="branch to push and open a PR for")
    p.add_argument("--title", required=True, help="PR title")
    body = p.add_mutually_exclusive_group()
    body.add_argument("--body-file", help="file holding the PR description (markdown)")
    body.add_argument("--body", default="", help="inline PR description")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("cleanup", help="remove worktrees + branches whose PRs have merged")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser(
        "abandon", help="tear down a worktree and delete its branch (local only, no GitHub)"
    )
    p.add_argument("branch", help="branch whose worktree + local branch to remove")
    p.set_defaults(func=cmd_abandon)

    return parser.parse_args()


def main(cfg: DevenvConfig):
    args = parse_args()
    try:
        args.func(cfg, args)
    except SetupException as e:
        for arg in e.args:
            print(arg, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
    main(load_config(Path(_toplevel)))
