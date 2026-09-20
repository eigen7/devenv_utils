"""Multi-repo workshops: the manifest, membership detection, same-path mounts.

A *workshop* is a repo that assembles component repos as reusable building
blocks -- one workshop might be faceswap + faceswap-training + videogen, and
another faceswap + something else. Its `workshop.toml` names the components
by git URL, and each is cloned into a directory inside the workshop, with its
mount dir alongside:

    my-workshop/            workshop.toml, the workshop repo's own files
      faceswap/             clone of the component (gitignored)
      faceswap-mount/       its data dir, MOUNT_DIR in faceswap/.env.json
      videogen/  videogen-mount/
      xfer/                 handoff area between components

    # workshop.toml
    name = "my-workshop"            # DNS label: it prefixes names, see below
    [components.faceswap]
    url = "git@github.com:someone/faceswap.git"
    branch = "main"                 # optional; default is the remote's HEAD
    [components.somelib]
    url = "..."
    container = false               # source-only: cloned, but no dev container

**Components know nothing about workshops.** Nothing about a workshop is
committed into a component, so the same repo can be a block in several
workshops (through a separate clone in each) or stand alone. Membership is
therefore detected from where a clone sits: `find_membership` walks up to the
nearest manifest and asks whether it lists a component at this path. A linked
worktree counts as its main clone.

Inside a workshop, `config.DevenvConfig.join_workshop` namespaces the
component's container name, image tag and default mount dir, and
`dev_container_args` mounts the workshop into the container, on top of the
component's own /workspace/repo and /workspace/mount, under two paths:

    -v /home/me/my-workshop:/workspace/workshop-mount   the short, stable path
    -v /home/me/my-workshop:/home/me/my-workshop        the same-path mount

The short path is what a human types: every component container reaches the
workshop at /workspace/workshop-mount, whatever the workshop is called or
where it was cloned. The same-path mount is what makes a *host* path mean the
same file inside the container, which is what lets components hand files to
each other by path (through xfer/) with no copying between mounts, and lets a
git worktree created on the host -- whose .git file records an absolute host
path -- resolve inside the container.

Any component mount dir kept outside the workshop (a per-machine choice to
share a data dir between workshops) gets a same-path mount too.

A component's own files are therefore reachable by more than one path, e.g.
/workspace/repo and /workspace/workshop-mount/<component>. They are the same
directory, not copies. Use /workspace/repo and /workspace/mount for the
component you are working in, and the other paths only to reach across
components.

Separate clones per workshop, rather than one shared checkout, are what let
each workshop hold its components on its own branches with its own containers.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .console import SetupException, print_red
from .state import get_env_json

MANIFEST = "workshop.toml"

# Where the workshop directory appears inside every component container, next
# to the component's own /workspace/repo and /workspace/mount. It is mounted
# here *and* at its host path (see dev_container_args).
CONTAINER_WORKSHOP_PATH = "/workspace/workshop-mount"

# The workshop name prefixes container names and gateway hostnames, so it must
# be a DNS label (the same rule config.py applies to project names).
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class Component:
    key: str
    path: Path
    url: str
    # The branch to track, or None to follow the remote's default branch.
    branch: str | None
    container: bool


@dataclass(frozen=True)
class Workshop:
    name: str
    root: Path
    components: tuple[Component, ...]


@dataclass(frozen=True)
class Membership:
    """A checkout's place in a workshop: which workshop, and as what."""

    workshop: Workshop
    component: Component


# Everything a [components.<key>] table may hold. An unknown key is usually a
# top-level key written after a table header, which TOML reads as part of that
# table -- silently changing nothing if it were ignored.
_COMPONENT_KEYS = frozenset({"url", "branch", "path", "container"})


def _parse_component(root: Path, key: str, table: dict) -> Component:
    unknown = sorted(set(table) - _COMPONENT_KEYS)
    if unknown:
        raise SetupException(
            f"{root / MANIFEST}: component {key!r} has unknown key(s) {', '.join(unknown)}. "
            f"A component takes {', '.join(sorted(_COMPONENT_KEYS))}; a workshop-level key "
            "must appear above the first [components.<name>] header."
        )
    path = (root / table.get("path", key)).resolve()
    if not path.is_relative_to(root):
        raise SetupException(f"{root / MANIFEST}: component {key!r} path {path} is outside it.")
    if "url" not in table:
        raise SetupException(f"{root / MANIFEST}: component {key!r} has no `url`.")
    return Component(
        key=key,
        path=path,
        url=table["url"],
        branch=table.get("branch"),
        container=table.get("container", True),
    )


def load_workshop(root: Path) -> Workshop:
    """Parse `root`/workshop.toml."""
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
    return Workshop(name=name, root=root, components=components)


def _main_checkout(repo_root: Path) -> Path:
    """The main clone a checkout belongs to. A linked worktree's `.git` is a file
    pointing at <main>/.git/worktrees/<id>; anything else is its own main."""
    dot_git = repo_root / ".git"
    if dot_git.is_file():
        gitdir = Path(dot_git.read_text().removeprefix("gitdir:").strip())
        if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
            return gitdir.parent.parent.parent.resolve()
    return repo_root.resolve()


def find_membership(repo_root: Path) -> Membership | None:
    """Where `repo_root` sits in a workshop, or None when standalone.

    Walks up from the checkout's main clone to the nearest manifest and looks
    for a component at that path. A worktree of a component clone counts as
    that component. A clone that merely sits inside a workshop directory
    without being listed is standalone."""
    checkout = _main_checkout(repo_root)
    for parent in checkout.parents:
        if (parent / MANIFEST).is_file():
            workshop = load_workshop(parent)
            for component in workshop.components:
                if component.path == checkout:
                    return Membership(workshop=workshop, component=component)
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


def same_path_mount_dirs(workshop: Workshop) -> list[Path]:
    """The host directories to bind-mount at their own paths: the workshop
    directory, plus each component's mount dir (the MOUNT_DIR in its .env.json)
    where that points outside the workshop -- a per-machine choice to share a
    data dir across workshops. Missing paths are reported and skipped."""
    candidates = [workshop.root]
    for component in workshop.components:
        mount_dir = get_env_json(component.path / ".env.json").get("MOUNT_DIR")
        if mount_dir:
            candidates.append(Path(mount_dir))
    paths = []
    for path in candidates:
        if path.is_dir():
            paths.append(path.resolve())
        else:
            print_red(f"Workshop path {path} does not exist; not mounting it.")
    return sorted(_outermost(paths))


def dev_container_args(workshop: Workshop) -> list[str]:
    """`docker run` args mounting the workshop: once at the fixed container
    path every component can rely on, and once (like every other directory
    here) at its own host path."""
    args = ["-v", f"{workshop.root}:{CONTAINER_WORKSHOP_PATH}"]
    for path in same_path_mount_dirs(workshop):
        args += ["-v", f"{path}:{path}"]
    return args
