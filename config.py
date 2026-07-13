"""DevenvConfig: the per-project knobs the generic devenv tooling needs.

A project constructs one of these (typically in its own small config module)
and hands it to SetupWizardTool and to the standalone run/build scripts. Only
`name` and `repo_root` are required; everything else derives a sensible default
from those, and can be overridden per project.
"""

from dataclasses import dataclass, field
from pathlib import Path


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

    # Ports forwarded host -> container by run_docker. For instance N (set via
    # "INSTANCE" in .env.json) each is shifted up by instance_port_stride * N.
    required_ports: list[int] = field(default_factory=list)
    # Per-instance port shift. Instance N forwards every required port plus
    # instance_port_stride * N; the same offset is pushed into the container as
    # DEVENV_INSTANCE_PORT_OFFSET. See instances.py.
    instance_port_stride: int = 100
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
