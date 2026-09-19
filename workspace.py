"""Workspace identity mounts: sibling projects visible at their host paths.

Several consumer repos can form a *workspace*: one directory holding a
`workspace.toml` that lists the member repos. A project opts in with the
`workspace` key in its devenv.toml (a path to that directory, relative to the
repo). Its dev container then additionally bind-mounts the workspace directory
and every member's repo and mount dir, each at its own host path. A host path
is then valid in every member's container, so files are handed between projects
by path, with no copying between mounts, and git worktrees created on the host
resolve inside the containers too.

These mounts come on top of the project's own /workspace/repo and
/workspace/mount, which are unchanged.

workspace.toml:

    # Member repos, relative to this file. Each member's mount dir is the
    # MOUNT_DIR recorded in its .env.json.
    members = ["../faceswap", "faceswap-training", "videogen"]
"""

import tomllib
from pathlib import Path

from .console import SetupException, print_red
from .state import get_env_json

WORKSPACE_FILE = "workspace.toml"


def _member_paths(workspace_root: Path, member: str) -> list[Path]:
    """A member's repo, plus its mount dir if its .env.json records one."""
    repo = workspace_root / member
    paths = [repo]
    mount_dir = get_env_json(repo / ".env.json").get("MOUNT_DIR")
    if mount_dir:
        paths.append(Path(mount_dir))
    return paths


def _outermost(paths: list[Path]) -> list[Path]:
    """Drop duplicates and any path inside another listed one; the enclosing
    bind mount already covers it."""
    kept = []
    for path in sorted(set(paths), key=lambda p: len(p.parts)):
        if not any(path.is_relative_to(parent) for parent in kept):
            kept.append(path)
    return kept


def workspace_paths(workspace_root: Path) -> list[Path]:
    """The resolved host paths to identity-mount for the workspace at
    `workspace_root`: the workspace directory itself, plus each member's repo and
    mount dir. Missing paths are reported and skipped, so one member that isn't
    set up doesn't block launching the others."""
    toml_path = workspace_root / WORKSPACE_FILE
    if not toml_path.is_file():
        raise SetupException(
            f"devenv.toml sets workspace = {workspace_root}, but {toml_path} does not exist."
        )
    members = tomllib.loads(toml_path.read_text()).get("members", [])
    candidates = [workspace_root]
    for member in members:
        candidates += _member_paths(workspace_root, member)
    paths = []
    for path in candidates:
        if path.is_dir():
            paths.append(path.resolve())
        else:
            print_red(f"Workspace path {path} does not exist; not mounting it.")
    return sorted(_outermost(paths))


def dev_container_args(workspace_root: Path) -> list[str]:
    """`docker run` args that bind-mount each workspace path at its own host path."""
    args = []
    for path in workspace_paths(workspace_root):
        args += ["-v", f"{path}:{path}"]
    return args
