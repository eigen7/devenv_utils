"""Multi-repo workspaces: detecting membership and the identity mounts.

A workspace is a repo that assembles component repos as building blocks
(WORKSPACES.md). Its `workspace.toml` names the components by URL, and each
component is cloned into a directory inside the workspace. Components know
nothing about workspaces: membership is detected here, from where a clone sits
(`find_membership`), never from anything committed in the component.

Inside workspace `W`, a component's dev container gets names namespaced by `W`
(see config.load_config), plus identity mounts: the workspace directory, and
any component mount dir outside it, each bind-mounted at its own host path.
Host paths are then valid in every component's container.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .console import SetupException, print_red
from .state import get_env_json

MANIFEST = "workspace.toml"

# The workspace name prefixes container names and gateway hostnames, so it must
# be a DNS label (the same rule config.py applies to project names).
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class Component:
    key: str
    path: Path
    url: str
    branch: str
    container: bool


@dataclass(frozen=True)
class Workspace:
    name: str
    root: Path
    components: tuple[Component, ...]


def _parse_component(root: Path, key: str, table: dict) -> Component:
    path = (root / table.get("path", key)).resolve()
    if not path.is_relative_to(root):
        raise SetupException(f"{root / MANIFEST}: component {key!r} path {path} is outside it.")
    return Component(
        key=key,
        path=path,
        url=table["url"],
        branch=table.get("branch", "main"),
        container=table.get("container", True),
    )


def load_workspace(root: Path) -> Workspace:
    """Parse `root`/workspace.toml."""
    root = root.resolve()
    data = tomllib.loads((root / MANIFEST).read_text())
    name = data.get("name", "")
    if not _NAME_RE.match(name):
        raise SetupException(
            f"{root / MANIFEST}: name {name!r} must match {_NAME_RE.pattern}; it prefixes "
            "container names and gateway hostnames."
        )
    components = tuple(
        _parse_component(root, key, table) for key, table in data.get("components", {}).items()
    )
    return Workspace(name=name, root=root, components=components)


def _main_checkout(repo_root: Path) -> Path:
    """The main clone a checkout belongs to. A linked worktree's `.git` is a file
    pointing at <main>/.git/worktrees/<id>; anything else is its own main."""
    dot_git = repo_root / ".git"
    if dot_git.is_file():
        gitdir = Path(dot_git.read_text().removeprefix("gitdir:").strip())
        if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
            return gitdir.parent.parent.parent.resolve()
    return repo_root.resolve()


def find_membership(repo_root: Path) -> tuple[Workspace, Component] | None:
    """The workspace `repo_root` belongs to, and its component entry; None when
    standalone. Walks up from the checkout's main clone to the nearest
    workspace.toml and looks for a component at that path. A worktree of a
    component clone counts as that component."""
    checkout = _main_checkout(repo_root)
    for parent in checkout.parents:
        if (parent / MANIFEST).is_file():
            workspace = load_workspace(parent)
            for component in workspace.components:
                if component.path == checkout:
                    return workspace, component
            return None
    return None


def _outermost(paths: list[Path]) -> list[Path]:
    """Drop duplicates and any path inside another listed one; the enclosing
    bind mount already covers it."""
    kept = []
    for path in sorted(set(paths), key=lambda p: len(p.parts)):
        if not any(path.is_relative_to(parent) for parent in kept):
            kept.append(path)
    return kept


def identity_mount_paths(workspace: Workspace) -> list[Path]:
    """The host paths to bind-mount at themselves: the workspace directory, plus
    each component's mount dir (the MOUNT_DIR in its .env.json) where that
    points outside it -- a per-machine choice to share a data dir across
    workspaces. Missing paths are reported and skipped."""
    candidates = [workspace.root]
    for component in workspace.components:
        mount_dir = get_env_json(component.path / ".env.json").get("MOUNT_DIR")
        if mount_dir:
            candidates.append(Path(mount_dir))
    paths = []
    for path in candidates:
        if path.is_dir():
            paths.append(path.resolve())
        else:
            print_red(f"Workspace path {path} does not exist; not mounting it.")
    return sorted(_outermost(paths))


def dev_container_args(workspace: Workspace) -> list[str]:
    """`docker run` args that bind-mount each identity path at itself."""
    args = []
    for path in identity_mount_paths(workspace):
        args += ["-v", f"{path}:{path}"]
    return args
