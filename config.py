"""DevenvConfig: the per-project knobs the generic devenv tooling needs.

A project constructs one of these (typically in its own small config module)
and hands it to SetupWizardTool and to the standalone run/build scripts. Only
`name` and `repo_root` are required; everything else derives a sensible default
from those, and can be overridden per project.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SubtreeSpec:
    """One vendored git subtree: where it lives and what it tracks upstream.

    `name` is the directory name under the project's subtrees root (the prefix
    is <subtrees_root>/<name>). git records neither the remote URL nor the
    tracked branch anywhere committed, so both must be declared here; `branch`
    defaults to "main".
    """

    name: str
    url: str
    branch: str = "main"


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
    default_mount_dir: Optional[Path] = None

    # Where the repo / mount dir are bind-mounted inside the container.
    # Set container_mount_path to None for projects that don't need a
    # persistent mount dir: the wizard's mount-dir step and run_docker's
    # MOUNT_DIR requirement are then skipped entirely.
    container_repo_path: str = "/workspace/repo"
    container_mount_path: Optional[str] = "/workspace/mount"

    # Ports forwarded host -> container by run_docker. For instance N (set via
    # "INSTANCE" in .env.json) each is shifted up by instance_port_stride * N.
    required_ports: list[int] = field(default_factory=list)
    # Per-instance port shift. Instance N forwards every required port plus
    # instance_port_stride * N; the same offset is pushed into the container as
    # DEVENV_INSTANCE_PORT_OFFSET. See instances.py.
    instance_port_stride: int = 100
    # Extra static args appended to every `docker run` (e.g. ["--ipc=host"]).
    extra_docker_args: list[str] = field(default_factory=list)
    # Minimum acceptable value of the image's `version` label.
    min_image_version: str = "0.0.0"
    # Unprivileged user the container runs as / VS Code attaches as.
    remote_user: str = "devuser"

    # Docker build context (holds the project Dockerfile). Defaults to
    # <repo_root>/docker-setup.
    docker_context: Optional[Path] = None
    # Where persisted user choices live. Defaults to <repo_root>/.env.json.
    env_json_path: Optional[Path] = None

    # Setup contract version stamped into .env.json by SetupWizardTool.commit()
    # on a successful run. Entrypoints can gate on it, and bumping the major
    # component triggers rm_target_on_major_bump(). The "0.0.0" default leaves
    # the mechanism inert for projects that don't use it.
    setup_version: str = "0.0.0"
    # Build output directory removed by rm_target_on_major_bump() on a major
    # setup_version bump. Defaults to <repo_root>/target.
    target_dir: Optional[Path] = None

    # Git subtrees vendored under the project's subtrees root, consumed by
    # DevTool.pull_git_subtrees / push_git_subtrees. Empty for projects that
    # don't vendor any.
    subtrees: list[SubtreeSpec] = field(default_factory=list)

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
