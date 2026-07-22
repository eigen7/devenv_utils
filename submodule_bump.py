"""Advance a superproject's recorded submodule pointer to the submodule's
Gitea `main` tip -- the shared logic behind two conveniences.

The recorded pointer (the gitlink in the superproject's HEAD) lags a
submodule's Gitea `main` whenever a submodule-only PR has merged but no
superproject commit has bumped the pointer yet. Two entry points offer to
close that gap and share everything here:

  * `git publish` (publish.py) offers the bump right before it pushes, so the
    push ships it.
  * the post-merge freshness hook (submodule_guard.py) reacts when a `git
    pull` brought in a merge.

They differ only in how they prompt; the freshness check, the spanned-commit
listing, the safety checks, and the bump-commit creation live here once.
"""

import re
import subprocess
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .config import load_config
from .gitea_client import REMOTE_NAME
from .pr_flow import submodule_pointer

GITLINK_MODE = "160000"


def git(cwd: Path, *args: str):
    subprocess.run(["git", *args], cwd=cwd, check=True)


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def git_result(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def short(sha: str) -> str:
    return sha[:7]


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
    """Whether `maybe_ancestor` is an ancestor of `of` in `repo`. A commit git
    cannot resolve (absent from the checkout) counts as not-an-ancestor."""
    return git_result(repo, "merge-base", "--is-ancestor", maybe_ancestor, of).returncode == 0


def checked_out_head(submodule: Path) -> str | None:
    """The submodule checkout's HEAD, or None when it is not populated."""
    result = git_result(submodule, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def has_uncommitted_changes(submodule: Path) -> bool:
    return bool(git_out(submodule, "status", "--porcelain", "--untracked-files=no").strip())


def submodule_gitea_tip(repo_root: Path, sub_path: str) -> str | None:
    """The submodule's Gitea `main` tip, fetched into the submodule clone so it
    is addressable locally. None when the submodule has no Gitea repo to derive
    a URL from, or that repo cannot be reached -- this is a convenience, never a
    blocker.
    """
    sub = repo_root / sub_path
    try:
        url = gitea_read_url(repo_root, sub_path)
    except subprocess.CalledProcessError:
        return None
    if git_result(sub, "fetch", "--quiet", url, "main").returncode != 0:
        return None
    return git_out(sub, "rev-parse", "FETCH_HEAD")


def bump_status(sub: Path, recorded: str, tip: str) -> str:
    """How the recorded pointer relates to the Gitea `tip`: 'none' (the pointer
    already contains the tip, or is ahead of it -- nothing to do), 'ahead' (the
    tip has commits the pointer lacks -- a bump is available), or 'diverged'."""
    if tip == recorded or is_ancestor(sub, tip, recorded):
        return "none"
    if is_ancestor(sub, recorded, tip):
        return "ahead"
    return "diverged"


def spanned_commits(sub: Path, recorded: str, tip: str) -> list[str]:
    """The commits in `recorded..tip`, newest first, as `<short-hash> <subject>`
    display lines."""
    out = git_out(sub, "log", "--format=%h %s", f"{recorded}..{tip}")
    return out.splitlines() if out else []


def unsafe_reason(name: str, sub: Path, recorded: str) -> str:
    """Why the submodule checkout cannot be safely bumped, or '' when it can. A
    dirty checkout or one not at the recorded pointer is left alone, so no work
    is discarded."""
    if has_uncommitted_changes(sub):
        return f"{name}: submodule checkout has uncommitted changes; leaving the pointer as is."
    if checked_out_head(sub) != recorded:
        return (
            f"{name}: submodule checkout is not at the recorded pointer; leaving the pointer as is."
        )
    return ""


@dataclass(frozen=True)
class BumpOffer:
    """The outcome of checking one submodule's recorded pointer against its
    Gitea `main`. `status` drives what a caller does; the remaining fields carry
    what a caller needs to prompt and to build the bump commit.

    status is one of:
      "none"     -- the pointer already contains the tip; do nothing silently.
      "diverged" -- the histories forked; skip and report `.warning`.
      "unsafe"   -- the checkout is dirty or off the recorded pointer; skip and
                    report `.warning`.
      "ready"    -- the tip is ahead of the pointer; offer or perform the bump.
    """

    status: str
    name: str
    sub_path: str
    recorded: str
    tip: str
    spanned: tuple[str, ...]
    warning: str


def evaluate_bump(repo_root: Path, name: str, sub_path: str) -> BumpOffer | None:
    """Check one submodule's recorded pointer against its Gitea `main` tip.

    Returns None when there is nothing to consider (no reachable Gitea repo for
    the submodule), otherwise a BumpOffer describing what to do.
    """
    tip = submodule_gitea_tip(repo_root, sub_path)
    if tip is None:
        return None
    recorded = submodule_pointer(repo_root, sub_path)
    sub = repo_root / sub_path
    status = bump_status(sub, recorded, tip)
    if status == "none":
        return BumpOffer("none", name, sub_path, recorded, tip, (), "")
    if status == "diverged":
        warning = f"{name}: submodule Gitea main has diverged from the recorded pointer; skipping."
        return BumpOffer("diverged", name, sub_path, recorded, tip, (), warning)
    warning = unsafe_reason(name, sub, recorded)
    if warning:
        return BumpOffer("unsafe", name, sub_path, recorded, tip, (), warning)
    spanned = tuple(spanned_commits(sub, recorded, tip))
    return BumpOffer("ready", name, sub_path, recorded, tip, spanned, "")


def bump_question(offer: BumpOffer) -> str:
    return f"Update {offer.name} submodule to latest [{short(offer.tip)}]?"


def bump_header(offer: BumpOffer) -> str:
    return f"{offer.name}: submodule Gitea main is ahead of the recorded pointer:"


def bump_commands_text(name: str, sub_path: str, tip: str) -> str:
    """The two commands a bump runs, for a `?` explanation's tail."""
    return (
        f"    git -C {sub_path} checkout {short(tip)}\n"
        f'    git commit -m "Bump {name} submodule to {short(tip)}" -- {sub_path}'
    )


def perform_note(name: str, tip: str) -> str:
    """The one-line notice printed after a bump is committed."""
    return f"Updated {name} submodule to {short(tip)} and committed the pointer bump on main."


def never_note(name: str, recorded: str, tip: str) -> str:
    """The one-line notice printed when a bump is available but not acted on."""
    return (
        f"Note: {name} has new upstream commits ({short(recorded)}..{short(tip)}). "
        'Set pull_update = "prompt" or "always" under [submodules] in devenv.toml '
        "to act on this during git pull."
    )


def bump_commit(offer: BumpOffer, repo_root: Path):
    """Check the submodule out at the tip and record the pointer bump on the
    superproject's `main`.

    Commits only the gitlink path -- `git commit -- <sub_path>` bypasses the
    index for every other path -- so unrelated staged or dirty files are left
    exactly as they are. The subject names the target; the body is the log of
    the commits the bump spans.
    """
    git(repo_root / offer.sub_path, "checkout", "--quiet", offer.tip)
    subject = f"Bump {offer.name} submodule to {short(offer.tip)}"
    message = subject + "\n\n" + "\n".join(offer.spanned) + "\n"
    git(repo_root, "commit", "-m", message, "--", offer.sub_path)


def pull_update_mode(repo_root: Path) -> str:
    """The [submodules] pull_update mode from devenv.toml, read leniently: a
    missing, unparseable, or invalid file falls back to "prompt", so a hook
    never breaks a `git pull` over a config problem."""
    try:
        return load_config(repo_root).submodules.pull_update
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return "prompt"


_SUBMODULES_TABLE_RE = re.compile(r"^\[submodules\][ \t]*$", re.MULTILINE)
_PULL_UPDATE_LINE_RE = re.compile(r"^([ \t]*pull_update[ \t]*=).*$", re.MULTILINE)


def save_pull_update_never(devenv_toml: Path):
    """Persist `pull_update = "never"` under `[submodules]` in devenv.toml,
    preserving every other byte.

    Appending a bare `key = value` at end-of-file would land the key inside
    whatever table happens to be last, so the write is table-aware: a
    `pull_update` line is rewritten in place, a bare `[submodules]` table gains
    the key under its header, and an absent table is appended whole.
    """
    text = devenv_toml.read_text()
    if _SUBMODULES_TABLE_RE.search(text):
        if _PULL_UPDATE_LINE_RE.search(text):
            text = _PULL_UPDATE_LINE_RE.sub(r'\1 "never"', text, count=1)
        else:
            text = _SUBMODULES_TABLE_RE.sub('[submodules]\npull_update = "never"', text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += '\n[submodules]\npull_update = "never"\n'
    devenv_toml.write_text(text)
