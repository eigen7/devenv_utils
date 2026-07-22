#!/usr/bin/env python3
"""Submodule pointer safety and working-tree sync -- part of the repo's
pre-commit hook, plus the post-checkout/post-merge sync action (installed by
SetupWizardTool.setup_git_config()).

Stock git leaves two gaps in the submodule model SUBMODULES.md describes;
each action here closes one:

  pre-commit (`pre-commit`): refuse a commit that moves a submodule pointer
      *backward*. The typical cause is a stale submodule checkout swept into
      the index by a broad `git add`: the older commit already exists
      upstream, so `push.recurseSubmodules=check` cannot catch it, and the
      rewind lands looking deliberate. The fix is to sync the checkout and
      re-stage; a genuinely intended rewind goes through with
      `git commit --no-verify`.

  sync (`sync`): update each populated submodule working tree to the commit
      the superproject records. `submodule.recurse=true` covers checkout and
      pull, but `git rebase` -- fast-forward or not -- leaves submodule
      working trees stale; running this from post-checkout and post-merge
      closes that gap. Sync never discards work: a submodule with
      uncommitted changes, or with commits the recorded pointer lacks, is
      left alone with a warning. Unpopulated submodules are also left alone
      -- populating a fresh worktree is pr_flow.py's job (it clones from the
      main checkout, covering pointers whose commit is not upstream yet),
      and setup_common self-heals fresh clones.

  offer-update (`offer-update`): from post-merge, when a `git pull` merged
      something, check each submodule's Gitea main against the recorded
      pointer and react per the [submodules] pull_update mode -- "prompt"
      (offer the bump over /dev/tty), "always" (bump without asking), or
      "never" (print a one-line note). Runs only on the `main` branch of a
      checkout with a `gitea` remote, and never fails the hook: post-merge
      cannot undo the merge, so every problem is at most a warning, and a
      non-interactive pull (no /dev/tty) prints the note rather than blocking.
      The bump logic itself is shared with `git publish` (see submodule_bump).
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Enable running this file directly (submodules/devenv_utils/submodule_guard.py):
    # put the repo root on sys.path and adopt the package identity so the
    # relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "submodules.devenv_utils"

import subprocess

from .commit_guard import guards_main
from .config import DevenvConfig, load_config
from .gitea_client import gitmodule_entries
from .submodule_bump import (
    BumpOffer,
    bump_commands_text,
    bump_commit,
    bump_header,
    bump_question,
    checked_out_head,
    evaluate_bump,
    has_uncommitted_changes,
    never_note,
    perform_note,
    pull_update_mode,
    save_pull_update_never,
)

GITLINK_MODE = "160000"

REWIND_MESSAGE = """\
This commit would move the submodule {path} backward:
{new} is an ancestor of the currently recorded {old}.
That usually means the submodule checkout is stale and a broad `git add`
staged it. To sync the checkout and re-stage the pointer:
    git submodule update --init {path}
    git add {path}
To rewind deliberately: git commit --no-verify"""


def git_result(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def warn(message: str):
    print(f"warning: {message}", file=sys.stderr)


def is_ancestor(repo: Path, maybe_ancestor: str, of: str) -> bool | None:
    """Whether `maybe_ancestor` is an ancestor of `of` in `repo`, or None when
    git cannot answer (e.g. one of the commits is absent from the checkout)."""
    rc = git_result(repo, "merge-base", "--is-ancestor", maybe_ancestor, of).returncode
    return {0: True, 1: False}.get(rc)


def staged_pointer_moves(repo_root: Path) -> list[tuple[str, str, str]]:
    """The staged submodule pointer changes, as (path, old_sha, new_sha).

    Pointer additions and submodule removals change the entry's mode away
    from gitlink-on-both-sides and are excluded: only a move between two
    commits can be a rewind.
    """
    moves = []
    for line in git_out(repo_root, "diff", "--cached", "--raw", "--no-renames").splitlines():
        meta, path = line.split("\t", 1)
        old_mode, new_mode, old_sha, new_sha, _status = meta.lstrip(":").split()
        if old_mode == GITLINK_MODE and new_mode == GITLINK_MODE:
            moves.append((path, old_sha, new_sha))
    return moves


def check(repo_root: Path):
    """The pre-commit action: block staged backward submodule pointer moves."""
    for path, old_sha, new_sha in staged_pointer_moves(repo_root):
        rewind = is_ancestor(repo_root / path, new_sha, old_sha)
        if rewind is None:
            warn(f"could not determine whether {path} moves backward; allowing commit.")
        elif rewind:
            sys.exit(REWIND_MESSAGE.format(path=path, old=old_sha[:7], new=new_sha[:7]))


def recorded_pointers(repo_root: Path) -> list[tuple[str, str]]:
    """Every submodule the index records, as (path, recorded_sha)."""
    pointers = []
    for line in git_out(repo_root, "ls-files", "-s").splitlines():
        meta, path = line.split("\t", 1)
        mode, sha, _stage = meta.split()
        if mode == GITLINK_MODE:
            pointers.append((path, sha))
    return pointers


def sync_one(repo_root: Path, path: str, recorded: str):
    """Bring one stale submodule checkout to the recorded pointer, or explain
    why it was left alone."""
    submodule = repo_root / path
    if has_uncommitted_changes(submodule):
        warn(f"{path} has uncommitted changes; leaving it at its current commit.")
        return
    if is_ancestor(submodule, checked_out_head(submodule), recorded) is not True:
        warn(f"{path} has commits the recorded pointer lacks; leaving it as checked out.")
        return
    result = git_result(repo_root, "submodule", "update", "--", path)
    if result.returncode == 0:
        print(f"synced {path} -> {recorded[:7]}")
    else:
        warn(f"could not sync {path}: {result.stderr.strip()}")


def sync(repo_root: Path):
    """The post-checkout/post-merge action: sync stale submodule checkouts."""
    for path, recorded in recorded_pointers(repo_root):
        head = checked_out_head(repo_root / path)
        if head is not None and head != recorded:
            sync_one(repo_root, path, recorded)


SAVE_QUESTION = 'You selected no. Save this selection for future "git pull" calls?'

SAVE_EXPLANATION = (
    'Answering Y writes pull_update = "never" under [submodules] in devenv.local.toml,\n'
    "the untracked local override. Future `git pull`s then print a one-line note when\n"
    "this submodule's Gitea main is ahead, instead of prompting."
)


def hook_bump_explanation(offer: BumpOffer) -> str:
    return (
        f"The {offer.name} submodule's Gitea main has commits the superproject's\n"
        "recorded pointer does not include yet -- typically a submodule PR that just\n"
        "merged. Answering Y checks the submodule out at that tip and commits the\n"
        "pointer bump on main; the post-commit hook mirrors it to your Gitea main.\n"
        "\n"
        "Proceeding with Y runs the commands:\n"
        "\n"
        f"{bump_commands_text(offer.name, offer.sub_path, offer.tip)}"
    )


def open_tty():
    """The controlling terminal, for prompting from inside a hook (stdin is not
    the terminal there). None when there is none -- a scripted, CI, or
    agent-driven pull -- so the caller falls back to a non-interactive note
    instead of blocking."""
    try:
        return open("/dev/tty", "r+")
    except OSError:
        return None


def tty_prompt(tty, question: str, explanation: str) -> bool:
    """A yes/no prompt over the terminal `tty`, defaulting to yes; `?` prints
    the explanation and asks again. Mirrors publish.confirm's house style with
    terminal I/O rather than stdin."""
    while True:
        tty.write(f"{question} [Y/n/?] ")
        tty.flush()
        answer = tty.readline().strip().lower()
        if answer == "?":
            tty.write(explanation + "\n")
            tty.flush()
        else:
            return answer not in ("n", "no")


def write_commits(tty, header: str, lines: list):
    tty.write(header + "\n")
    for line in lines:
        tty.write(f"  {line}\n")
    tty.flush()


def perform_bump(repo_root: Path, offer: BumpOffer):
    bump_commit(offer, repo_root)
    print(perform_note(offer.name, offer.tip))


def prompt_bump(repo_root: Path, offer: BumpOffer):
    """Offer the bump over the terminal. Declining offers to persist "never" so
    future pulls stop asking. With no terminal, print the note and return --
    a non-interactive pull must never block."""
    tty = open_tty()
    if tty is None:
        print(never_note(offer.name, offer.recorded, offer.tip))
        return
    try:
        write_commits(tty, bump_header(offer), list(offer.spanned))
        if tty_prompt(tty, bump_question(offer), hook_bump_explanation(offer)):
            perform_bump(repo_root, offer)
        elif tty_prompt(tty, SAVE_QUESTION, SAVE_EXPLANATION):
            save_pull_update_never(repo_root / "devenv.local.toml")
            print('Wrote pull_update = "never" to devenv.local.toml (untracked local override).')
    finally:
        tty.close()


def react_to_bump(repo_root: Path, name: str, sub_path: str, mode: str):
    """React to one submodule's freshness per `mode`: nothing when the pointer
    is current, a warning when it cannot be bumped safely, else the mode's
    action (note / bump / prompt)."""
    offer = evaluate_bump(repo_root, name, sub_path)
    if offer is None or offer.status == "none":
        return
    if offer.status in ("diverged", "unsafe"):
        warn(offer.warning)
        return
    if mode == "never":
        print(never_note(offer.name, offer.recorded, offer.tip))
    elif mode == "always":
        perform_bump(repo_root, offer)
    else:
        prompt_bump(repo_root, offer)


def offer_update(repo_root: Path):
    """The post-merge freshness action: react to each submodule whose Gitea main
    is ahead of the recorded pointer. A no-op off `main` or without a `gitea`
    remote. Never fails the hook -- a merge cannot be undone -- so any per-
    submodule error is downgraded to a warning."""
    if not guards_main(repo_root):
        return
    mode = pull_update_mode(repo_root)
    for name, sub_path in gitmodule_entries(repo_root):
        try:
            react_to_bump(repo_root, name, sub_path, mode)
        except Exception as err:  # noqa: BLE001 -- a hook must never break the pull.
            warn(f"{name}: could not check submodule freshness: {err}")


ACTIONS = {
    "pre-commit": check,
    "sync": sync,
    "offer-update": offer_update,
}


def main(cfg: DevenvConfig):
    ACTIONS[sys.argv[1]](cfg.repo_root)


if __name__ == "__main__":
    main(load_config(Path(__file__).resolve().parents[2]))
