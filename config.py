"""DevenvConfig: the per-project knobs the generic devenv tooling needs.

A project constructs one of these (typically in its own small config module)
and hands it to SetupWizardTool and to the standalone run/build scripts. Only
`name` and `repo_root` are required; everything else derives a sensible default
from those, and can be overridden per project.

`load_config()` builds one from a repo-root `devenv.toml`, so a generic entry
point can construct the config from the file's location alone -- without
importing any project Python.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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


# How `git pull` reacts when a submodule's Gitea main has advanced past the
# recorded pointer (see submodule_bump.py). "prompt" offers the bump
# interactively, "never" prints a one-line note, "always" bumps without asking.
PULL_UPDATE_MODES = ("prompt", "never", "always")


@dataclass(frozen=True)
class Submodules:
    """The [submodules] table: submodule-workflow knobs. `pull_update` chooses
    how a `git pull` reacts to a submodule whose Gitea main is ahead of the
    recorded pointer -- one of PULL_UPDATE_MODES."""

    pull_update: str = "prompt"


def _coerce_submodules(value) -> Submodules:
    """Normalize a [submodules] value -- a {pull_update} table or an already-built
    Submodules -- into a Submodules, rejecting an out-of-range pull_update."""
    if isinstance(value, Submodules):
        return value
    if isinstance(value, dict):
        mode = value.get("pull_update", "prompt")
        if mode not in PULL_UPDATE_MODES:
            raise ValueError(f"pull_update {mode!r} must be one of {PULL_UPDATE_MODES}")
        return Submodules(pull_update=mode)
    raise ValueError(f"[submodules] {value!r} must be a table")


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
    # Suggested host mount dir during setup. Defaults to ~/<name>.
    default_mount_dir: Path | None = None

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
    # The [submodules] table of submodule-workflow knobs (see Submodules).
    submodules: "Submodules" = field(default_factory=Submodules)
    # Extra static args appended to every `docker run` (e.g. ["--ipc=host"]).
    extra_docker_args: list[str] = field(default_factory=list)
    # Unprivileged user the container runs as / VS Code attaches as.
    remote_user: str = "devuser"

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
    # Directory holding the PR workflow's per-task git worktrees (pr_flow.py).
    # Defaults to <container_mount_path>/worktrees/<name>, so projects sharing
    # a mount cannot collide. Meaningful only inside the container, and only
    # for projects with a mount dir.
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
            self.default_mount_dir = Path.home() / self.name
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
            self.worktrees_dir = Path(self.container_mount_path) / "worktrees" / self.name
        if self.worktrees_dir is not None:
            self.worktrees_dir = Path(self.worktrees_dir)
        self.services = {name: _coerce_service(v) for name, v in self.services.items()}
        self.submodules = _coerce_submodules(self.submodules)
        if self.services:
            _validate_dns_label("project name", self.name)
            for service_name in self.services:
                _validate_dns_label("service name", service_name)


# devenv.toml keys whose values are paths, resolved relative to the repo root.
_PATH_FIELDS = frozenset(
    {"docker_context", "env_json_path", "target_dir", "default_mount_dir", "worktrees_dir"}
)


def _toml_kwargs(repo_root: Path, toml_path: Path) -> dict:
    """The DevenvConfig kwargs a single TOML file contributes: each top-level key
    as-is, with path-valued keys resolved relative to `repo_root`."""
    data = tomllib.loads(toml_path.read_text())
    return {k: (repo_root / v if k in _PATH_FIELDS else v) for k, v in data.items()}


def load_config(repo_root: Path) -> DevenvConfig:
    """Build a DevenvConfig from `repo_root`/devenv.toml, overlaid by an optional
    `repo_root`/devenv.local.toml.

    devenv.toml holds the static DevenvConfig fields as data, so a generic
    devenv_utils entry point can construct the config knowing only where the
    file lives -- no project Python to import. Each key maps to a DevenvConfig
    field; path-valued keys are resolved relative to `repo_root`, and
    `repo_root` itself always comes from the caller (the file's directory),
    never from the file.

    devenv.local.toml, when present, is an untracked per-checkout override with
    the same schema. Its top-level keys replace devenv.toml's wholesale -- a
    table such as [submodules] supersedes the tracked one entirely, not
    key-by-key -- so a developer can pin local-only settings without touching
    the tracked file.
    """
    kwargs = _toml_kwargs(repo_root, repo_root / "devenv.toml")
    local = repo_root / "devenv.local.toml"
    if local.exists():
        kwargs.update(_toml_kwargs(repo_root, local))
    return DevenvConfig(repo_root=repo_root, **kwargs)
