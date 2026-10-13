"""DevenvConfig: the per-project knobs the generic devenv tooling needs.

A project constructs one of these (typically in its own small config module)
and hands it to SetupWizardTool and to the standalone run/build scripts. Only
`name` and `repo_root` are required; everything else derives a sensible default
from those, and can be overridden per project.

`load_config()` builds one from a repo-root `devenv.toml`, so a generic entry
point can construct the config from the file's location alone -- without
importing any project Python.
"""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .shared_mount import DEFAULT_SHARED_MOUNT_ROOT

# Where devenv_utils itself is developed: the source of every consumer's
# subtrees/devenv_utils/, and of the wizard's working clone.
DEVENV_UTILS_REPO_URL = "https://github.com/eigen7/devenv_utils.git"

# Service names and the project name become DNS labels (under `.localhost`) and
# env-var suffixes, so they are restricted to lowercase letters, digits, and
# hyphens, starting with a letter.
_DNS_LABEL_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class Service:
    """One entry of the [services] table: a container port the gateway routes
    to. `publish` additionally publishes 127.0.0.1:<port>:<port> for non-HTTP
    traffic that hostname routing cannot carry (see GATEWAY.md)."""

    port: int
    publish: bool = False


def _coerce_service(value) -> Service:
    """Normalize a [services] value -- an int, a {port, publish} table, or an
    already-built Service -- into a Service."""
    if isinstance(value, Service):
        return value
    # bool is an int subclass, so reject it before the int branch below.
    if isinstance(value, int) and not isinstance(value, bool):
        return Service(port=value)
    if isinstance(value, dict):
        return Service(port=value["port"], publish=value.get("publish", False))
    raise ValueError(f"service value {value!r} must be an int port or {{port, publish}} table")


def _validate_dns_label(kind: str, value: str):
    if not _DNS_LABEL_RE.match(value):
        raise ValueError(
            f"{kind} {value!r} must match {_DNS_LABEL_RE.pattern}: it becomes a DNS "
            "label under .localhost and an env-var suffix."
        )


@dataclass
class DevenvConfig:
    name: str
    repo_root: Path

    # Local Docker image tag. Defaults to `name`.
    image: str = ""
    # Container name used by run_docker / the VS Code attach config.
    # Defaults to f"{name}_instance".
    instance_name: str = ""
    # Hostname inside the container. Defaults to f"{name}-container".
    container_hostname: str = ""
    # Suggested host mount dir during setup. Defaults to ~/<name>, or to
    # DEFAULT_SHARED_MOUNT_ROOT for a shared_mount project.
    default_mount_dir: Path | None = None
    # Whether MOUNT_DIR is a shared root that a family of projects all mount
    # whole at container_mount_path, each keeping its own data under
    # <root>/<name> (see shared_mount.py). Off, MOUNT_DIR is this project's
    # alone and is itself its data dir.
    shared_mount: bool = False

    # Where the repo / mount dir are bind-mounted inside the container.
    # Set container_mount_path to None for projects that don't need a
    # persistent mount dir: the wizard's mount-dir step and run_docker's
    # MOUNT_DIR requirement are then skipped entirely.
    container_repo_path: str = "/workspace/repo"
    container_mount_path: str | None = "/workspace/mount"

    # The named [services] table: service name -> container port, routed by the
    # gateway as http://<project>-<service>.localhost (see GATEWAY.md). Each
    # value is an int port, or the table form {port, publish} where publish=true
    # additionally publishes 127.0.0.1:<port>:<port> for non-HTTP traffic.
    services: dict[str, Service] = field(default_factory=dict)
    # Extra static args appended to every `docker run` (e.g. ["--ipc=host"]).
    extra_docker_args: list[str] = field(default_factory=list)
    # Unprivileged user the container runs as / VS Code attaches as.
    remote_user: str = "devuser"
    # Claude Code release baked into the image: a channel ("latest"/"stable"),
    # resolved to its current version at build time, or an exact version pin.
    # Only used if the Dockerfile declares `ARG CLAUDE_CODE_VERSION`.
    claude_code_version: str = "latest"

    # Docker build context (holds the project Dockerfile). Defaults to
    # <repo_root>/docker-setup.
    docker_context: Path | None = None
    # Where persisted user choices live. Defaults to <repo_root>/.env.json.
    env_json_path: Path | None = None

    # Setup contract version stamped into .env.json by SetupWizardTool.commit()
    # on a successful run. Entrypoints can gate on it via check_setup_version(),
    # so bumping it forces users back through the wizard -- the way to roll out
    # any setup-side change, including Dockerfile changes that need an image
    # rebuild. Bumping the major component additionally triggers
    # rm_target_on_major_bump(). The "0.0.0" default leaves the mechanism inert
    # for projects that don't use it.
    setup_version: str = "0.0.0"
    # Build output directory removed by rm_target_on_major_bump() on a major
    # setup_version bump. Defaults to <repo_root>/target.
    target_dir: Path | None = None
    # The [subtrees] table: each vendored subtree (subtrees/<name>/) -> the git
    # URL of the repo it is pulled from (pull_subtrees.py). git subtree records
    # no source, and a subtree's owner need not be the consumer's, so each is
    # named here; devenv_utils, which every consumer vendors, is always known
    # (DEVENV_UTILS_REPO_URL) unless overridden.
    subtrees: dict[str, str] = field(default_factory=dict)
    # Directory holding the PR workflow's per-task git worktrees (pr_flow.py).
    # Defaults to <project mount>/worktrees/<name>, where the project mount is
    # $DEVENV_PROJECT_MOUNT (set by run_container) or else
    # project_mount_path. The env var places the worktrees of a repo with no
    # container of its own -- the devenv_utils working clone -- under the
    # data dir of whichever project's container it is used from. Meaningful
    # only inside the container, and only for projects with a mount dir.
    worktrees_dir: Path | None = None

    def __post_init__(self):
        self.repo_root = Path(self.repo_root)
        if not self.image:
            self.image = self.name
        if not self.instance_name:
            self.instance_name = f"{self.name}_instance"
        if not self.container_hostname:
            self.container_hostname = f"{self.name}-container"
        if self.default_mount_dir is None:
            self.default_mount_dir = (
                DEFAULT_SHARED_MOUNT_ROOT if self.shared_mount else Path.home() / self.name
            )
        self.default_mount_dir = Path(self.default_mount_dir)
        if self.docker_context is None:
            self.docker_context = self.repo_root / "docker-setup"
        self.docker_context = Path(self.docker_context)
        if self.env_json_path is None:
            self.env_json_path = self.repo_root / ".env.json"
        self.env_json_path = Path(self.env_json_path)
        if self.target_dir is None:
            self.target_dir = self.repo_root / "target"
        self.target_dir = Path(self.target_dir)
        if self.worktrees_dir is None and self.container_mount_path is not None:
            project_mount = os.environ.get("DEVENV_PROJECT_MOUNT") or self.project_mount_path
            self.worktrees_dir = Path(project_mount) / "worktrees" / self.name
        if self.worktrees_dir is not None:
            self.worktrees_dir = Path(self.worktrees_dir)
        self.subtrees = {"devenv_utils": DEVENV_UTILS_REPO_URL, **self.subtrees}
        self.services = {name: _coerce_service(v) for name, v in self.services.items()}
        if self.services:
            _validate_dns_label("project name", self.name)
            for service_name in self.services:
                _validate_dns_label("service name", service_name)

    @property
    def project_mount_path(self) -> Path | None:
        """This project's own data dir inside the container: its subdir of the
        shared root, or the whole mount for a standalone project."""
        if self.container_mount_path is None:
            return None
        return self.project_mount_dir(Path(self.container_mount_path))

    def project_mount_dir(self, mount_dir: os.PathLike) -> Path:
        """This project's own data dir, given the mount dir (MOUNT_DIR on the
        host, container_mount_path inside the container)."""
        mount_dir = Path(mount_dir)
        return mount_dir / self.name if self.shared_mount else mount_dir


# devenv.toml keys whose values are paths, resolved relative to the repo root.
_PATH_FIELDS = frozenset(
    {"docker_context", "env_json_path", "target_dir", "default_mount_dir", "worktrees_dir"}
)


def load_config(repo_root: Path) -> DevenvConfig:
    """Build a DevenvConfig from `repo_root`/devenv.toml.

    devenv.toml holds the static DevenvConfig fields as data, so a generic
    devenv_utils entry point can construct the config knowing only where the
    file lives -- no project Python to import. Each key maps to a DevenvConfig
    field; path-valued keys are resolved relative to `repo_root`, and
    `repo_root` itself always comes from the caller (the file's directory),
    never from the file.
    """
    data = tomllib.loads((repo_root / "devenv.toml").read_text())
    kwargs = {k: (repo_root / v if k in _PATH_FIELDS else v) for k, v in data.items()}
    return DevenvConfig(repo_root=repo_root, **kwargs)
