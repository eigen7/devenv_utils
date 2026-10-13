"""The shared mount: one host directory bind-mounted whole by a family of projects.

A project that opts in (`shared_mount = true` in devenv.toml) records the
shared root as MOUNT_DIR and keeps its own data under <root>/<name>/. Every
container of the family mounts the whole root at the same container path, so
one project's outputs (say, a trained model) are directly readable by another
at /workspace/mount/<other-project>/..., with no copying between mounts.

The root carries a layout version file. A MOUNT_DIR without it is a
pre-shared, per-project mount: the wizard migrates it by moving it whole into
<root>/<name>/ (see migrate_into).

This module is pure filesystem logic with explicit paths; the wizard
(wizard.py) owns the prompting.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

from .console import SetupException

# Bump when the root's layout changes, and teach root_problem() and the
# wizard how to migrate the previous version forward.
SHARED_MOUNT_VERSION = 1

VERSION_FILE = ".devenv-mount-version"

DEFAULT_SHARED_MOUNT_ROOT = Path.home() / "eigen7-mount"


def read_version(root: Path) -> int | None:
    """The root's layout version, or None if it has no version file."""
    path = Path(root) / VERSION_FILE
    if not path.is_file():
        return None
    return int(path.read_text().strip())


def is_current(root: Path) -> bool:
    return read_version(root) == SHARED_MOUNT_VERSION


def root_problem(root: Path) -> str | None:
    """Why `root` can't serve as the shared root, or None if it can.

    A usable root is either already at the current version, or absent/empty
    (init_root makes it one). Anything else is some other directory's data.
    """
    root = Path(root)
    version = read_version(root)
    if version == SHARED_MOUNT_VERSION:
        return None
    if version is not None:
        return f"{root} is a shared mount at layout version {version}, expected {SHARED_MOUNT_VERSION}."
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        return f"{root} is not empty and has no {VERSION_FILE}, so it is not a shared mount root."
    return None


def init_root(root: Path):
    """Create `root` (if needed) and stamp it with the current layout version."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / VERSION_FILE).write_text(f"{SHARED_MOUNT_VERSION}\n")


@dataclass(frozen=True)
class MountMove:
    """Where a migrated mount was and is, as seen from the host and from inside
    the container (where it was bound at the container mount path and is now
    a subdirectory of it)."""

    host_old: str
    host_new: str
    container_old: str
    container_new: str

    def remap(self, path: str) -> str | None:
        """`path` re-pointed into the new location, or None if it was not under the old one."""
        for old, new in ((self.host_old, self.host_new), (self.container_old, self.container_new)):
            if path == old or path.startswith(old + "/"):
                return new + path[len(old) :]
        return None

    def on_host(self, path: str) -> Path:
        """A post-move path (host- or container-written) as a host path."""
        if path.startswith(self.container_new + "/"):
            return Path(self.host_new + path[len(self.container_new) :])
        return Path(path)


def migrate_into(move: MountMove, git_dirs: list[Path]):
    """Move the pre-shared mount whole from move.host_old to move.host_new,
    then repair the git worktree links the move broke in each repo whose
    common git dir is in `git_dirs`."""
    old, dest = Path(move.host_old), Path(move.host_new)
    if dest.exists():
        if any(dest.iterdir()):
            raise SetupException(
                f"Cannot migrate {old} into {dest}: {dest} already has contents.",
                "Merge the two by hand, then re-run the wizard.",
            )
        dest.rmdir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Moving {old} -> {dest} ...")
    shutil.move(str(old), str(dest))
    for git_dir in git_dirs:
        _rewrite_worktree_links(Path(git_dir), move)


def _rewrite_worktree_links(git_dir: Path, move: MountMove):
    """Re-point both ends of each worktree link of the repo at `git_dir`.

    git ties a worktree to its repo with two absolute paths: the repo's
    <git_dir>/worktrees/<id>/gitdir names the worktree's .git file, and that
    .git file names <git_dir>/worktrees/<id> back. Either end may lie under
    the moved mount, written from the host or from a container; `git worktree
    repair` only fixes paths valid where it runs, so the prefixes are
    rewritten here directly.
    """
    worktrees = git_dir / "worktrees"
    if not worktrees.is_dir():
        return
    for gitdir_file in worktrees.glob("*/gitdir"):
        worktree_git = gitdir_file.read_text().strip()
        moved = move.remap(worktree_git)
        if moved is not None:
            worktree_git = moved
            gitdir_file.write_text(worktree_git + "\n")
        _rewrite_backlink(move.on_host(worktree_git), move)


def _rewrite_backlink(worktree_git: Path, move: MountMove):
    """Re-point a worktree's .git file at its repo's moved git dir, if it moved."""
    if not worktree_git.is_file():
        return
    backlink = move.remap(worktree_git.read_text().removeprefix("gitdir:").strip())
    if backlink is not None:
        worktree_git.write_text(f"gitdir: {backlink}\n")
